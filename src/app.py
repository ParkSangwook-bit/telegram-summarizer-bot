import os
import re
import html
import json
import asyncio
import time
import hmac
import hashlib
import socket
import ipaddress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin, quote
import httpx
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update # telegram ui용
from google import genai
from google.genai import types
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# 설정 파일
import settings

# ===============================================================
# 초기화
# ===============================================================

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(settings.AWSConfig.DYNAMO_TABLE)
lambda_client = boto3.client('lambda')  # 유튜브 요약 작업 함수 호출용

if settings.AIConfig.API_KEY:
    client = genai.Client(
        api_key=settings.AIConfig.API_KEY,
        http_options=types.HttpOptions(timeout=settings.AIConfig.TIMEOUT_MS),
    )
    # temperature 등은 지정하지 않음: Gemini 3 계열은 기본값(1.0) 유지가 권장됨
    generate_config = types.GenerateContentConfig(
        system_instruction=settings.PromptConfig.SYSTEM_TEMPLATE,
        thinking_config=types.ThinkingConfig(thinking_level=settings.AIConfig.THINKING_LEVEL),
        safety_settings=settings.AIConfig.SAFETY_SETTINGS,
    )
else:
    client = None

# ===============================================================
# 헬퍼 함수 (DB & Logic)
# ===============================================================

def is_duplicate_request(update_id):
    """
    args:
        update_id (int): 텔레그램 업데이트 ID
    features:
        AWS Lambda 재시도로 인한 중복 실행 방지
    """
    try:
        # TTL: 24시간
        ttl_seconds = int(time.time()) + (24 * 60 * 60)
        table.put_item(
            Item={
                'chat_id': 'SYSTEM_PROCESSED_UPDATES',
                'timestamp': str(update_id),
                'message_id': 0, # 스키마 호환성을 위한 더미 값
                'ttl': ttl_seconds
            },
            # chat_id가 파티션 키이므로, 동일 ID가 없을 때만 기록
            ConditionExpression='attribute_not_exists(chat_id)' 
        )
        return False
    
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return True
        print(f"Deduplication Error: {e}")
        return False

def save_message_to_db(chat_id, message_id, user_name, text, message_date, media=None):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
        message_id (int): 텔레그램 메시지 ID
        user_name (str): 사용자 이름
        text (str): 메시지 내용
        message_date (int): 메시지 날짜
        media (dict): 사진·음성의 파일 정보 (요약 때 Gemini에 함께 보냄)
    features:
        메시지를 DynamoDB에 저장
    """
    KST = timezone(timedelta(hours=9))

    try:
        if isinstance(message_date, (int, float)):
            timestamp = datetime.fromtimestamp(message_date, tz=KST).isoformat()
        else:
            timestamp = datetime.now(KST).isoformat()

        # TTL: 7일
        ttl_seconds = int(time.time()) + (7 * 24 * 60 * 60)

        item = {
            'chat_id': str(chat_id),
            'timestamp': timestamp,
            'message_id': int(message_id), # 중복 제거용
            'user_name': user_name,
            'message': text,
            'ttl': ttl_seconds
        }
        if media:
            item.update(media)
        table.put_item(Item=item)
        return item

    except Exception as e:
        print(f"DB Save Error: {e}")
        return None

# ===============================================================
# 작업 함수: 유튜브 영상 요약 · 사진/음성 설명 (올라오면 미리 처리해 DB에 저장)
# ===============================================================

YOUTUBE_PATTERN = re.compile(r"(?:youtube\.com/(?:watch\?\S*?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
VIDEO_PARTITION = 'VIDEO_SUMMARY'  # 영상 요약 캐시 (대화와 섞이지 않게 별도 파티션)

def invoke_worker(payload):
    """ 작업 함수를 비동기로 호출 (웹훅 응답은 바로 끝남). 작업 함수가 없으면(로컬) 아무것도 안 함 """
    worker = os.environ.get("MEDIA_WORKER_FUNCTION")
    if worker:
        lambda_client.invoke(FunctionName=worker, InvocationType='Event', Payload=json.dumps(payload))

def request_media_description(item):
    """ 저장한 메시지가 사진·짧은 음성이면 설명·받아쓰기를 작업 함수에 맡김 """
    if not item or item.get('media_type') not in ('photo', 'voice'):
        return
    if item['media_type'] == 'voice' and int(item.get('duration', 0)) > settings.MediaConfig.VOICE_MAX_SECONDS:
        return
    invoke_worker({'chat_id': item['chat_id'], 'timestamp': item['timestamp'],
                   'file_id': item['file_id'], 'media_type': item['media_type']})

def youtube_ids(text):
    """ 글 속 유튜브 영상 ID (순서 유지, 중복 제거) """
    return list(dict.fromkeys(YOUTUBE_PATTERN.findall(text)))

def request_video_summaries(text):
    """ 글 속 유튜브 영상마다 요약을 한 번만 작업 함수에 맡김 """
    if not os.environ.get("MEDIA_WORKER_FUNCTION"):
        return
    for video_id in youtube_ids(text):
        try:
            # 이미 맡겼거나 요약이 있으면 건너뜀 (1일 뒤 만료 -> 작업이 죽었으면 다시 맡길 수 있음)
            table.put_item(
                Item={'chat_id': VIDEO_PARTITION, 'timestamp': video_id, 'message_id': 0,
                      'status': 'pending', 'ttl': int(time.time()) + 24 * 60 * 60},
                ConditionExpression='attribute_not_exists(chat_id)'
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                print(f"Video Request Error: {e}")
            continue
        invoke_worker({'video_id': video_id})

def get_video_summaries(video_ids):
    """ {영상 ID: 요약} (요약이 끝난 것만) """
    if not video_ids:
        return {}
    try:
        response = dynamodb.batch_get_item(RequestItems={settings.AWSConfig.DYNAMO_TABLE: {
            'Keys': [{'chat_id': VIDEO_PARTITION, 'timestamp': video_id} for video_id in video_ids]}})
        items = response.get('Responses', {}).get(settings.AWSConfig.DYNAMO_TABLE, [])
        return {item['timestamp']: item['summary'] for item in items if item.get('status') == 'done' and item.get('summary')}
    except Exception as e:
        print(f"Video Summary Query Error: {e}")
        return {}

def log_model_verdict(where, response):
    """ 모델의 종료 사유·안전 등급·차단 사유를 로그에 남김 (순화인지 차단인지 가리기 위함, 내용은 남기지 않음) """
    try:
        candidates = getattr(response, "candidates", None) or []
        candidate = candidates[0] if candidates else None
        finish = getattr(getattr(candidate, "finish_reason", None), "name", None)
        ratings = [f"{r.category.name.replace('HARM_CATEGORY_', '')}:{r.probability.name}"
                   for r in (getattr(candidate, "safety_ratings", None) or []) if r.category and r.probability]
        feedback = getattr(response, "prompt_feedback", None)
        block = getattr(getattr(feedback, "block_reason", None), "name", None)
        print(f"Model Verdict [{where}] finish={finish} block={block} ratings={ratings}")
    except Exception as e:
        print(f"Model Verdict [{where}] unreadable: {e}")

def media_worker_handler(event, context):
    """ 작업 함수 진입점 (요약 봇이 비동기로 호출): 유튜브 영상 요약 또는 사진·음성 설명 """
    if 'video_id' in event:
        return summarize_video(event['video_id'])
    return describe_media_file(event)

async def download_telegram_file(file_id):
    async with telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN")) as bot:
        tg_file = await bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

def describe_media_file(event):
    """ 텔레그램에서 사진·음성을 받아 설명·받아쓰기를 만들고, 그 메시지 항목에 media_text로 저장 """
    config = settings.MediaConfig
    is_photo = event['media_type'] == 'photo'
    try:
        data = asyncio.run(download_telegram_file(event['file_id']))
        response = client.models.generate_content(
            model=settings.AIConfig.MODEL_NAME,
            contents=[types.Part.from_bytes(data=data, mime_type='image/jpeg' if is_photo else 'audio/ogg'),
                      config.DESCRIBE_PHOTO if is_photo else config.DESCRIBE_VOICE],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level=settings.AIConfig.THINKING_LEVEL),
                safety_settings=settings.AIConfig.SAFETY_SETTINGS,
                http_options=types.HttpOptions(timeout=config.TIMEOUT_MS),
            ),
        )
        log_model_verdict(event['media_type'], response)
        text = clip((response.text or "").strip(), config.TEXT_LENGTH)
        if not text:
            return {'status': 'failed'}
        table.update_item(
            Key={'chat_id': event['chat_id'], 'timestamp': event['timestamp']},
            UpdateExpression='SET media_text = :t',
            ExpressionAttributeValues={':t': text},
            ConditionExpression='attribute_exists(chat_id)',  # 메시지가 이미 지워졌으면 새로 만들지 않음
        )
        return {'status': 'done'}
    except Exception as e:
        print(f"Media Describe Error: {event.get('media_type')}: {e}")
        return {'status': 'failed'}

def summarize_video(video_id):
    """ 유튜브 영상을 요약해 VIDEO_SUMMARY 파티션에 저장 """
    item = {'chat_id': VIDEO_PARTITION, 'timestamp': video_id, 'message_id': 0}
    try:
        config = settings.VideoConfig
        response = client.models.generate_content(
            model=settings.AIConfig.MODEL_NAME,
            contents=[
                types.Part(file_data=types.FileData(file_uri=f"https://www.youtube.com/watch?v={video_id}", mime_type="video/*"),
                           video_metadata=types.VideoMetadata(fps=config.FPS, end_offset=f"{config.MAX_SECONDS}s")),
                config.PROMPT,
            ],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level=settings.AIConfig.THINKING_LEVEL),
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,  # 내용 파악엔 저해상도로 충분
                safety_settings=settings.AIConfig.SAFETY_SETTINGS,
                http_options=types.HttpOptions(timeout=config.TIMEOUT_MS),
            ),
        )
        log_model_verdict("video", response)
        summary = (response.text or "").strip()
        item.update(status='done' if summary else 'failed', summary=clip(summary, config.SUMMARY_LENGTH),
                    ttl=int(time.time()) + config.TTL_DAYS * 24 * 60 * 60)
    except Exception as e:
        print(f"Video Summary Error: {video_id}: {e}")
        item.update(status='failed', error=str(e)[:200], ttl=int(time.time()) + 24 * 60 * 60)
    table.put_item(Item=item)
    return {'video_id': video_id, 'status': item['status']}

def get_clean_chat_history(chat_id, limit=100):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
        limit (int): 가져올 메시지 수
    features:
        DynamoDB에서 메시지 히스토리를 가져오고 중복을 제거
    """
    try:
        # DB에서 여유있게 가져옴
        response = table.query(
            KeyConditionExpression=Key('chat_id').eq(str(chat_id)),
            ScanIndexForward=False, # 최신순 조회
            Limit=int(limit * 1.5)
        )
        items = response.get('Items', [])
        
        # 시간순 정렬 (과거 -> 현재): 수정본 덮어쓰기 위함
        items.sort(key=lambda x: x['timestamp'])
        
        # 딕셔너리 덮어쓰기
        deduplicated = {}
        for item in items:
            # message_id가 있으면 키로 사용하고, 없으면 timestamp 사용
            key = item.get('message_id', item['timestamp'])
            deduplicated[key] = item
            
        # 리스트로 변환 후 반환
        clean_list = list(deduplicated.values())
        return clean_list[-limit:] # 최신 N개만 반환
        
    except Exception as e:
        print(f"DB Query Error: {e}")
        return []

def format_messages_to_xml(messages):
    """
    args:
        messages (list): 메시지 딕셔너리 리스트
    features:
        메시지 경계와 화자를 분명히 하는 XML 변환
        (참고: 토큰은 "이름: 말" 줄 형식보다 많음. 2026-10-11 실측, 메시지 100개 기준 2,399 vs 821)
    """
    xml = "<logs>\n"
    current_date = ""

    for msg in messages:
        # msg['timestamp'] 예: '2025-11-18T14:30:00'
        date_part, time_part = msg['timestamp'].split('T')
        time_str = time_part[:5] # 예: 14:30

        # 날짜가 바뀌었을 때만 <d> 태그 추가 (토큰 절약 + 정보 보존)
        if date_part != current_date:
            xml += f'<d>{date_part}</d>\n'
            current_date = date_part
        
        # 이름이나 내용에 <>태그가 있으면 무력화 -> 시스템 프롬프트의 xml 태그 방어 로직
        user = html.escape(msg['user_name'])
        text = html.escape(msg['message'])
        xml += f'<l u="{user}" t="{time_str}">{text}</l>\n'
        
    xml += "</logs>"

    return xml

def format_header(messages):
    """
    args:
        messages (list): 시간순 메시지 딕셔너리 리스트
    features:
        요약 머리줄(메시지 수 + 시간 범위)을 코드에서 만듦 -> 모델이 날짜를 지어내지 않게 함
    """
    def short(timestamp):
        date_part, time_part = timestamp.split('T')
        _, month, day = date_part.split('-')
        return f"{int(month)}/{int(day)}", time_part[:5]

    first_day, first_time = short(messages[0]['timestamp'])
    last_day, last_time = short(messages[-1]['timestamp'])
    last = last_time if first_day == last_day else f"{last_day} {last_time}"
    return f"📌 최근 {len(messages)}개 대화 · {first_day} {first_time} ~ {last}"

def to_telegram_html(text):
    """
    args:
        text (str): 모델 출력
    features:
        전부 이스케이프한 뒤, 프롬프트가 허용한 태그만 되살림 (<b>, <i>, <code>, <blockquote [expandable]>)
    """
    safe = html.escape(text)
    for tag in ("b", "i", "code", "blockquote"):
        safe = safe.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return safe.replace("&lt;blockquote expandable&gt;", "<blockquote expandable>")

def strip_tags(text):
    """ HTML 전송이 실패했을 때 일반 텍스트로 보내기 위해 허용 태그를 걷어냄 """
    return re.sub(r"</?(b|i|code|blockquote)( expandable)?>", "", text)

DETAIL_TIME = re.compile(r"^•\s*\[(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\]\s*")

def sort_details_by_time(text, last_timestamp):
    """
    args:
        text (str): 모델 출력
        last_timestamp (str): 대화의 마지막 메시지 시각 (연도 계산용)
    features:
        접힌 상세의 각 줄 앞 [MM/DD HH:MM](그 화제가 처음 나온 시각)으로 줄을 시간순 정렬하고 시각 표시는 지움
        -> 모델이 중요도·최근순으로 섞어도 늘 "오래된 것 위, 최근 것 아래" (시각 없는 줄이 있으면 순서는 그대로)
    """
    block = re.search(r"(<blockquote expandable>)(.*?)(</blockquote>)", text, re.S)
    if not block:
        return text
    last = datetime.fromisoformat(last_timestamp)
    lines, keys = [], []
    for line in (l.strip() for l in block.group(2).strip().splitlines() if l.strip()):
        m = DETAIL_TIME.match(line)
        if m:
            month, day, hour, minute = map(int, m.groups())
            year = last.year if (month, day) <= (last.month, last.day) else last.year - 1  # 연말연시 대비
            keys.append((year, month, day, hour, minute))
            line = "• " + line[m.end():]
        else:
            keys.append(None)
        lines.append(line)
    if None not in keys:
        lines = [line for _, _, line in sorted(zip(keys, range(len(lines)), lines))]
    return text[:block.start()] + block.group(1) + "\n".join(lines) + block.group(3) + text[block.end():]

def to_seconds(duration):
    """ 텔레그램 duration(초 정수 또는 timedelta)을 초로 """
    return int(duration.total_seconds()) if hasattr(duration, "total_seconds") else int(duration or 0)

def format_duration(duration):
    """ 텔레그램 duration을 "m:ss"로 """
    seconds = to_seconds(duration)
    return f"{seconds // 60}:{seconds % 60:02d}"

def media_file(message):
    """ 요약 때 Gemini에 함께 보낼 사진·음성의 파일 정보. 해당 없으면 None """
    if message.photo:
        # 긴 변이 1280px 이하인 것 중 가장 큰 크기 (없으면 가장 작은 크기)
        fitting = [p for p in message.photo if max(p.width, p.height) <= 1280]
        photo = fitting[-1] if fitting else message.photo[0]
        return {'media_type': 'photo', 'file_id': photo.file_id}
    if message.voice:
        return {'media_type': 'voice', 'file_id': message.voice.file_id, 'duration': to_seconds(message.voice.duration)}
    return None

def attach_media_text(messages):
    """ 작업 함수가 미리 만든 사진 설명·음성 받아쓰기를 표식 뒤에 붙인 사본 """
    return [dict(m, message=f"{m['message']} — 내용: {m['media_text']}") if m.get('media_text') else m for m in messages]

def pick_media(messages):
    """ 아직 설명이 없는(방금 올라와 처리 중이거나 실패한) 최근 사진·짧은 음성 몇 개 (시간순) """
    config = settings.MediaConfig
    pending = [m for m in messages if not m.get('media_text')]
    photos = [m for m in pending if m.get('media_type') == 'photo'][-config.PHOTO_LIMIT:]
    voices = [m for m in pending if m.get('media_type') == 'voice' and int(m.get('duration', 0)) <= config.VOICE_MAX_SECONDS][-config.VOICE_LIMIT:]
    picked = {m['message_id'] for m in photos + voices}
    return [m for m in messages if m['message_id'] in picked]

async def download_media(bot, picked):
    """ 텔레그램에서 파일을 병렬로 받음. [(메시지, bytes)] (실패·시간 초과는 빠짐) """
    async def one(msg):
        try:
            tg_file = await bot.get_file(msg['file_id'])
            return msg, bytes(await tg_file.download_as_bytearray())
        except Exception as e:
            print(f"Media Download Error: {e}")
            return msg, None

    if not picked:
        return []
    tasks = [asyncio.create_task(one(msg)) for msg in picked]
    done, pending = await asyncio.wait(tasks, timeout=settings.MediaConfig.TOTAL_TIMEOUT)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    results = {id(task): task.result() for task in done}
    return [results[id(task)] for task in tasks if id(task) in results and results[id(task)][1]]

def attach_media(messages, downloaded):
    """
    features:
        받은 미디어에 번호를 매겨 대화 속 표식("[사진]" -> "[사진 #1]")과 Gemini에 보낼 조각을 만듦
    returns:
        (번호가 붙은 메시지 사본, [라벨, Part, 라벨, Part, ...])
    """
    labels, parts, counts = {}, [], {'photo': 0, 'voice': 0}
    for msg, data in downloaded:
        kind = msg['media_type']
        counts[kind] += 1
        labels[msg['message_id']] = counts[kind]
        name = '사진' if kind == 'photo' else '음성'
        parts += [f"[{name} #{counts[kind]}]",
                  types.Part.from_bytes(data=data, mime_type='image/jpeg' if kind == 'photo' else 'audio/ogg')]
    numbered = [dict(m, message=re.sub(r"^\[(사진|음성[^\]]*)\]", lambda t: f"[{t.group(1)} #{labels[m['message_id']]}]", m['message'], count=1))
                if m['message_id'] in labels else m for m in messages]
    return numbered, parts

def describe_media(message):
    """
    args:
        message (telegram.Message): 텔레그램 메시지
    features:
        글이 없는 메시지(사진·동영상·스티커 등)를 "[사진] 캡션" 같은 표식으로 바꿈 -> 요약 맥락에 포함
        (파일 내용 자체는 읽지 않음. 해당 없으면 None)
    """
    if message.photo:
        kind = "사진"
    elif message.animation:  # GIF는 document도 함께 채워지므로 먼저 확인
        kind = "GIF"
    elif message.video:
        kind = f"동영상 {format_duration(message.video.duration)}"
    elif message.video_note:
        kind = f"영상 메시지 {format_duration(message.video_note.duration)}"
    elif message.voice:
        kind = f"음성 {format_duration(message.voice.duration)}"
    elif message.audio:
        kind = f"오디오 {message.audio.title or ''}"
    elif message.sticker:
        kind = f"스티커 {message.sticker.emoji or ''}"
    elif message.document:
        kind = f"파일 {message.document.file_name or ''}"
    elif message.poll:
        options = " / ".join(option.text for option in message.poll.options)
        kind = f"투표 {message.poll.question} ({options})"
    elif message.venue:  # venue도 location을 함께 가지므로 먼저 확인
        kind = f"장소 {message.venue.title}"
    elif message.location:
        kind = "위치"
    elif message.contact:
        kind = "연락처"
    elif message.dice:
        kind = f"주사위 {message.dice.emoji} {message.dice.value}"
    else:
        return None

    tag = f"[{kind.strip()}]"
    return f"{tag} {message.caption}" if message.caption else tag

# ===============================================================
# 링크 제목 가져오기 (요약할 때만, 최근 링크 몇 개)
# ===============================================================

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
LINK_HEADERS = {"User-Agent": f"TelegramSummaryBot/{settings.BOT_VERSION} (+{settings.GITHUB_URL})"}
TITLE_PATTERNS = [re.compile(p, re.I | re.S) for p in (
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title',
    r"<title[^>]*>(.*?)</title>",
)]
DESC_PATTERNS = [re.compile(p, re.I | re.S) for p in (
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description',
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
)]

def clean_url(url):
    """ 문장 부호가 붙어 잡힌 끝부분을 떼어냄 ("(https://a.b/c)." -> "https://a.b/c") """
    url = url.rstrip(".,;:!?…'\"")
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url

def recent_links(messages, limit):
    """ 최근 메시지부터 중복 없이 링크 limit개 """
    links = []
    for msg in reversed(messages):
        for url in map(clean_url, URL_PATTERN.findall(msg['message'])):
            if url not in links:
                links.append(url)
    return links[:limit]

def is_public_url(url):
    """
    features:
        http(s)·기본 포트·공개 IP로만 풀리는 주소만 허용 (SSRF 방어)
        -> 람다 내부 런타임 API(127.0.0.1:9001) 같은 곳에 접속하지 않게 함
    """
    try:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.port not in (None, 80, 443):
            return False
        addresses = {info[4][0].split('%')[0] for info in socket.getaddrinfo(parts.hostname, None)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (ValueError, OSError):
        return False

def clip(text, length):
    text = " ".join(html.unescape(text).split())
    return text if len(text) <= length else text[:length - 1] + "…"

async def fetch_link_title(http, url):
    """ 링크 하나의 "제목 — 설명". 실패하면 None """
    host = urlparse(url).hostname or ""
    if host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com"):
        # 유튜브는 oEmbed로 제목·채널을 받음
        r = await http.get(f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json")
        if r.status_code != 200:
            return None
        data = r.json()
        return clip(f"{data.get('title', '')} — {data.get('author_name', '')}", settings.LinkConfig.TITLE_LENGTH)

    for _ in range(3):  # 리다이렉트는 단계마다 주소를 다시 검사
        if not await asyncio.to_thread(is_public_url, url):
            return None
        async with http.stream("GET", url) as r:
            if r.is_redirect and "location" in r.headers:
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return None
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) >= settings.LinkConfig.MAX_BYTES:  # 앞부분(head)만 읽음
                    break
            page = body.decode(r.encoding or "utf-8", errors="replace")
        title = next((m.group(1) for p in TITLE_PATTERNS if (m := p.search(page))), None)
        if not title:
            return None
        desc = next((m.group(1) for p in DESC_PATTERNS if (m := p.search(page))), None)
        return clip(f"{title} — {desc}" if desc else title, settings.LinkConfig.TITLE_LENGTH)
    return None

async def fetch_link_titles(urls):
    """ 링크들의 제목을 병렬로 가져옴. {url: "제목 — 설명"} (실패·시간 초과한 링크는 빠짐) """
    if not urls:
        return {}

    async def one(http, url):
        try:
            return url, await fetch_link_title(http, url)
        except Exception:
            return url, None

    async with httpx.AsyncClient(timeout=settings.LinkConfig.TIMEOUT, headers=LINK_HEADERS, follow_redirects=False) as http:
        tasks = [asyncio.create_task(one(http, url)) for url in urls]
        done, pending = await asyncio.wait(tasks, timeout=settings.LinkConfig.TOTAL_TIMEOUT)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    return {url: title for url, title in (task.result() for task in done) if title}

def attach_link_titles(messages, titles):
    """ 메시지 속 링크 뒤에 [링크: 제목 — 설명]을 붙인 사본 (DB 원본은 그대로) """
    if not titles:
        return messages
    attached = []
    for msg in messages:
        found = [titles[url] for url in map(clean_url, URL_PATTERN.findall(msg['message'])) if url in titles]
        attached.append(dict(msg, message=msg['message'] + "".join(f" [링크: {t}]" for t in found)) if found else msg)
    return attached

def add_video_summaries(titles, links):
    """ 유튜브 링크의 제목 뒤에 미리 만들어 둔 영상 요약을 덧붙임 (titles를 고쳐서 돌려줌) """
    ids = {url: youtube_ids(url)[0] for url in links if youtube_ids(url)}
    summaries = get_video_summaries(sorted(set(ids.values())))
    for url, video_id in ids.items():
        if video_id in summaries:
            titles[url] = f"{titles.get(url, 'YouTube 영상')} / 영상 요약: {summaries[video_id]}"
    return titles

# ===============================================================
# 핸들러 함수 (Command Handlers)
# ===============================================================

# 요약 아래 [최근 N개로 다시] 버튼에 쓸 개수 (이슈 #2: 숫자 입력 대신 버튼)
MORE_LIMITS = (200, 500)
MAX_LIMIT = 500

def more_buttons(limit):
    """ 지금보다 큰 개수만 버튼으로. 더 키울 게 없으면 None """
    buttons = [InlineKeyboardButton(f"최근 {n}개로 다시", callback_data=f"sum:{n}") for n in MORE_LIMITS if n > limit]
    return InlineKeyboardMarkup([buttons]) if buttons else None

async def handle_summary(update, context):
    """ /sum 명령어 처리 """
    # 숫자 파싱 (/sum 50)
    limit = 100
    parts = update.effective_message.text.split()
    if len(parts) > 1 and parts[1].isdigit():
        limit = min(int(parts[1]), MAX_LIMIT)  # 안전장치

    await send_summary(context.bot, update.effective_chat.id, limit)

async def handle_more_button(update, bot):
    """ 요약 아래 [최근 N개로 다시] 버튼 처리 """
    query = update.callback_query
    await query.answer()  # 버튼의 로딩 표시를 끔

    data = query.data or ""
    if data.startswith("sum:") and data[4:].isdigit():
        await send_summary(bot, query.message.chat.id, min(int(data[4:]), MAX_LIMIT))

async def send_summary(bot, chat_id, limit):
    """ 최근 limit개를 요약해 보냄 (명령어와 버튼이 함께 씀) """
    #TODO: 나중에 입력중으로 변경(입력중 + 안내 메시지)
    await bot.send_message(chat_id=chat_id, text=f"🤖 최근 {limit}개의 대화 내용을 읽고 있어요...")

    # DB 조회 -> 중복 제거 -> XML 변환
    chat_history = get_clean_chat_history(chat_id, limit)

    if not chat_history:
        await bot.send_message(chat_id=chat_id, text="요약할 대화 내용이 없습니다.")
        return

    # 링크 제목·설명과 사진·음성을 동시에 가져옴 (실패해도 요약은 진행)
    links = recent_links(chat_history, settings.LinkConfig.LIMIT)
    titles, downloaded = await asyncio.gather(fetch_link_titles(links), download_media(bot, pick_media(chat_history)))
    titles = add_video_summaries(titles, links)  # 유튜브는 미리 만들어 둔 영상 요약도 덧붙임
    # 사진·음성은 미리 만든 설명을 붙이고, 아직 설명이 없는 것만 파일을 직접 첨부
    messages, media_parts = attach_media(attach_link_titles(attach_media_text(chat_history), titles), downloaded)
    xml_data = format_messages_to_xml(messages)

    # AI 호출
    try:

        # AI에게 요약 생성 요청 (대화 XML 뒤에 번호 붙은 사진·음성)
        response = client.models.generate_content(
            model=settings.AIConfig.MODEL_NAME,
            contents=[xml_data, *media_parts],
            config=generate_config,
        )

        log_model_verdict("summary", response)
        response_text = (response.text or "").strip()

        # 안전 필터 등으로 본문이 비어 오는 경우
        if not response_text:
            await bot.send_message(chat_id=chat_id, text="❌ 요약을 만들지 못했습니다. 잠시 뒤 다시 시도해 주세요.")
            return

        # 접힌 상세를 화제가 처음 나온 시간순으로 (최근이 아래)
        response_text = sort_details_by_time(response_text, chat_history[-1]['timestamp'])

        header = format_header(chat_history)
        reply_markup = more_buttons(limit)

        # 안전장치
        try:
            # HTML 모드로 전송
            await bot.send_message(
                chat_id=chat_id,
                text=f"{html.escape(header)}\n\n{to_telegram_html(response_text)}",
                parse_mode='HTML',
                reply_markup=reply_markup,
            )
        except telegram.error.BadRequest:
            # HTML 파싱 에러 발생 시 -> 태그를 걷어낸 일반 텍스트로 재전송(전송 성공 보장)
            await bot.send_message(
                chat_id=chat_id,
                text=f"{header}\n\n{strip_tags(response_text)}",
                parse_mode=None,
                reply_markup=reply_markup,
            )

    except Exception as e:
        # AI 생성 실패 or 네트워크 오류 등 기타 치명적 에러
        await bot.send_message(chat_id=chat_id, text=f"❌ 시스템 오류: {e}")

async def handle_about(update, context):
    """ /about 명령어: 봇 정보와 버튼 출력 """
    chat_id = update.effective_chat.id

    about_text = (
        f"🤖 <b>Telegram Summary Bot</b> ({settings.BOT_VERSION})\n\n"
        "바쁜 당신을 위해 대화 내용을 놓치지 않도록\n"
        "<b>AI(Gemini)</b>가 핵심만 쏙쏙 요약해 드립니다.\n\n"
        f"<b>현재 AI모델</b>: {settings.AIConfig.MODEL_NAME}\n"
        "✨ <b>주요 기능</b>\n"
        "- <code>/sum</code>: 최근 100개 대화 요약 (<code>/sum 200</code>처럼 개수 지정, 최대 500)\n"
        "- 요약 아래 버튼으로 더 많은 대화를 다시 요약\n"
        "- 사진·음성은 내용까지 읽고, 동영상·파일은 표식과 캡션으로 요약에 포함\n"
        "- 링크는 페이지 제목·설명을, 유튜브는 영상 요약까지 읽어 요약에 포함\n"
        "- 대화 자동 저장 및 7일 뒤 삭제 (TTL)\n\n"
        f"📅 <b>마지막 업데이트:</b> {settings.LAST_UPDATE}"
    )

    keyboard = [
        [
            InlineKeyboardButton("🐙 깃허브 저장소 방문", url=settings.GITHUB_URL),
        ],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=about_text,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True # 링크 미리보기 끄기
    )

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 정보 출력 실패: {e}")

# help는 텔레그램 봇 안내로도 가능하지 않을까 검토중
# async def handle_help(update, context):
#     """ /help 명령어 처리 """
#     help_text = (
#         "🤖 **요약 봇 사용법**\n\n"
#         "- `/summary`: 최근 100개 대화 요약\n"
#         "- `/summary 50`: 최근 50개 대화 요약\n"
#         "- 그 외 대화는 자동으로 기록됩니다."
#     )
#     await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text, parse_mode='Markdown')

# ===============================================================
# 메인 로직 (Router)
# ===============================================================

# 딕셔너리 기반 라우터 설정
COMMAND_HANDLERS = {
    "/sum": handle_summary,
    "/summary": handle_summary,  # 예전 안내 문구(/summary)로 들어오는 경우
    "/about": handle_about,
    # "/help": handle_help,
    }

async def main_logic(event, context):
    bot = telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN"))
    
    async with bot:
        try:
            body = json.loads(event.get("body", "{}"))
            update = telegram.Update.de_json(body, bot)
            
            # 중복 방지
            if update.update_id and is_duplicate_request(update.update_id):
                print(f"Duplicate request ignored: {update.update_id}")
                return {"statusCode": 200, "body": "Duplicate ignored"}

            # 요약 아래 버튼을 누른 경우 (메시지가 아니므로 저장하지 않음)
            if update.callback_query:
                await handle_more_button(update, bot)
                return {"statusCode": 200, "body": "OK"}

            message = update.effective_message
            if not message:
                return {"statusCode": 200, "body": "No message"}

            # 글이 없는 메시지(사진·동영상·스티커 등)는 "[사진] 캡션" 같은 표식으로 저장
            text = message.text or describe_media(message)
            if not text:
                return {"statusCode": 200, "body": "No text message"}

            chat_id = message.chat.id
            user = message.from_user

            if user and user.is_bot:
                return {"statusCode": 200, "body": "Bot ignored"}

            # [라우팅 로직]

            # TODO: 지금은 임시방편으로 @봇이름 붙는 것 처리했음. 개선 필요
            # command_key = text.split()[0] if text.startswith("/") else None
            command_key = None
            
            # 딕셔너리에 있는 명령어면 실행

            if text.startswith("/"):
                # 공백 기준으로 첫 단어만 가져옴 ("/sum@bot 50" -> "/sum@bot")
                first_word = text.split()[0]
                
                # @가 붙어있으면 떼버림 ("/sum@bot" -> "/sum")
                command_key = first_word.split('@')[0]


            if command_key in COMMAND_HANDLERS:
                # duck typing: 진짜 CallbackContext가 아니므로, args, job_queue 등 속성없음. -> 나중에 라이브서버같은 polling으로 변경할 때 수정 필요.
                class Context: pass
                context_ex = Context()
                context_ex.bot = bot
                
                await COMMAND_HANDLERS[command_key](update, context_ex)
                
            # 존재하지않는 명령어는 무시 (DB 오염 방지)
            elif text.startswith("/"):
                pass 
                
            # 일반 대화는 저장
            else:
                item = save_message_to_db(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    # 익명 관리자·채널 글은 from_user가 없으므로 보낸 채팅 이름으로 대신함
                    user_name=user.first_name if user else (message.sender_chat.title if message.sender_chat else "알 수 없음"),
                    text=text,
                    message_date=message.date,
                    media=media_file(message)
                )
                # 사진·음성은 설명을, 유튜브 링크는 영상 요약을 미리 맡김 (작업 함수가 비동기로 처리)
                request_media_description(item)
                request_video_summaries(text)

        except Exception as e:
            print(f"Error: {e}")
            return {"statusCode": 200, "body": str(e)}

    return {"statusCode": 200, "body": "OK"}

def webhook_secret():
    """ 웹훅 비밀 토큰 = 봇 토큰에서 유도 (따로 보관할 비밀이 늘지 않음, setWebhook의 secret_token과 같은 값) """
    return hmac.new(os.environ.get("TELEGRAM_TOKEN", "").encode(), b"telegram-webhook-secret", hashlib.sha256).hexdigest()

def is_from_telegram(event):
    """ 텔레그램이 보낸 요청인지: X-Telegram-Bot-Api-Secret-Token 머리값 확인 """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return hmac.compare_digest(headers.get("x-telegram-bot-api-secret-token") or "", webhook_secret())

def lambda_handler(event, context):
    # 텔레그램이 아닌 곳에서 온 요청(위조 업데이트)은 처리하지 않음
    if not is_from_telegram(event):
        print("Rejected: missing or wrong webhook secret")
        return {"statusCode": 403, "body": "Forbidden"}
    return asyncio.run(main_logic(event, context))