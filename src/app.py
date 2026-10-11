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

def save_message_to_db(chat_id, message_id, user_name, text, message_date, media=None, user_id=None):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
        message_id (int): 텔레그램 메시지 ID
        user_name (str): 사용자 이름
        text (str): 메시지 내용
        message_date (int): 메시지 날짜
        media (dict): 사진·음성의 파일 정보 (요약 때 Gemini에 함께 보냄)
        user_id (int): 보낸 사람 번호 (멤버 제외·사람별 기능용. 이름은 겹치거나 바뀔 수 있음)
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
        if user_id is not None:
            item['user_id'] = int(user_id)
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

def request_media_description(item, media=None):
    """ 저장한 메시지가 사진·짧은 음성이면 설명·받아쓰기를 작업 함수에 맡김 (방 설정에서 끈 종류는 건너뜀) """
    if not item or item.get('media_type') not in ('photo', 'voice'):
        return
    if media and not media.get(item['media_type'], True):
        return
    if item['media_type'] == 'voice' and int(item.get('duration', 0)) > settings.MediaConfig.VOICE_MAX_SECONDS:
        return
    invoke_worker({'chat_id': item['chat_id'], 'timestamp': item['timestamp'],
                   'file_id': item['file_id'], 'media_type': item['media_type']})

def youtube_ids(text):
    """ 글 속 유튜브 영상 ID (순서 유지, 중복 제거) """
    return list(dict.fromkeys(YOUTUBE_PATTERN.findall(text)))

def request_video_summaries(text, media=None):
    """ 글 속 유튜브 영상마다 요약을 한 번만 작업 함수에 맡김 (방 설정에서 끄면 건너뜀) """
    if not os.environ.get("MEDIA_WORKER_FUNCTION") or (media and not media.get('youtube', True)):
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
    """ 작업 함수 진입점 (요약 봇이 비동기로 호출): 답장 상세 읽기 / 유튜브 영상 요약 / 사진·음성 설명 """
    if event.get('task') == 'detail':
        return handle_detail(event)
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

# ---------------------------------------------------------------
# 답장으로 부른 상세 읽기 (작업 함수)
# ---------------------------------------------------------------

STRIP_BLOCKS = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1>", re.I | re.S)

def fetch_page_text(url):
    """ 웹페이지 본문 글자 (링크 제목과 같은 SSRF 방어·리다이렉트 재검사). 실패하면 "" """
    with httpx.Client(timeout=10.0, headers=LINK_HEADERS, follow_redirects=False) as http:
        for _ in range(3):
            if not is_public_url(url):
                return ""
            with http.stream("GET", url) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                    return ""
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= 2_000_000:
                        break
                page = body.decode(r.encoding or "utf-8", errors="replace")
            text = html.unescape(re.sub(r"<[^>]+>", " ", STRIP_BLOCKS.sub(" ", page)))
            return " ".join(text.split())[:settings.DetailConfig.PAGE_MAX_CHARS]
    return ""

def describe_in_detail(event):
    """ 상세 읽기 본문(모델 출력): 유튜브 영상 / 웹페이지 / 사진 / 음성 """
    config = settings.DetailConfig
    kind = event['kind']
    extra = {}
    if kind == 'youtube':
        video = settings.VideoConfig
        contents = [types.Part(file_data=types.FileData(file_uri=f"https://www.youtube.com/watch?v={youtube_ids(event['url'])[0]}", mime_type="video/*"),
                               video_metadata=types.VideoMetadata(fps=video.FPS, end_offset=f"{video.MAX_SECONDS}s")),
                    config.YOUTUBE]
        extra['media_resolution'] = types.MediaResolution.MEDIA_RESOLUTION_LOW
    elif kind == 'page':
        text = fetch_page_text(event['url'])
        if len(text) >= config.PAGE_MIN_CHARS:
            contents = [f"{config.PAGE}\n\n<page url=\"{html.escape(event['url'])}\">\n{html.escape(text)}\n</page>"]
        else:
            # 로그인 벽·스크립트로만 그리는 페이지 등은 Gemini URL 읽기 도구로 한 번 더
            contents = [f"{config.PAGE_BY_URL}\n{event['url']}"]
            extra['tools'] = [types.Tool(url_context=types.UrlContext())]
    else:
        is_photo = kind == 'photo'
        data = asyncio.run(download_telegram_file(event['file_id']))
        contents = [types.Part.from_bytes(data=data, mime_type='image/jpeg' if is_photo else 'audio/ogg'),
                    config.PHOTO if is_photo else config.VOICE]
    response = client.models.generate_content(
        model=settings.AIConfig.MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=settings.AIConfig.THINKING_LEVEL),
            safety_settings=settings.AIConfig.SAFETY_SETTINGS,
            http_options=types.HttpOptions(timeout=settings.VideoConfig.TIMEOUT_MS),
            **extra,
        ),
    )
    log_model_verdict(f"detail-{kind}", response)
    return (response.text or "").strip()

async def send_reply(chat_id, reply_to, text):
    """ 그 /sum 명령에 답장으로 보냄 (HTML이 깨지면 태그를 걷어낸 글로) """
    async with telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN")) as bot:
        reply = telegram.ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        try:
            await bot.send_message(chat_id=chat_id, text=to_telegram_html(text), parse_mode='HTML', reply_parameters=reply)
        except telegram.error.BadRequest:
            await bot.send_message(chat_id=chat_id, text=strip_tags(text), reply_parameters=reply)

def handle_detail(event):
    """ 답장으로 부른 상세 읽기: 결과를 그 명령에 답장으로 보냄 """
    try:
        text = describe_in_detail(event)
    except Exception as e:
        print(f"Detail Error: {event.get('kind')}: {e}")
        text = ""
    status = 'done' if text else 'failed'
    text = (text or "❌ 자세히 읽지 못했습니다.")[:settings.DetailConfig.REPLY_MAX_CHARS]
    asyncio.run(send_reply(event['chat_id'], event['reply_to'], text))
    return {'status': status}

def get_clean_chat_history(chat_id, limit=100, since=None):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
        limit (int): 가져올 메시지 수
        since (str): 이 시각(ISO, KST) 이후만 (답장한 지점부터 요약할 때)
    features:
        DynamoDB에서 메시지 히스토리를 가져오고 중복을 제거
    """
    try:
        condition = Key('chat_id').eq(str(chat_id))
        if since:
            condition = condition & Key('timestamp').gte(since)
        # DB에서 여유있게 가져옴
        response = table.query(
            KeyConditionExpression=condition,
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

def format_header(messages, label="최근"):
    """
    args:
        messages (list): 시간순 메시지 딕셔너리 리스트
        label (str): 범위 이름 ("최근", "답장한 지점부터" 등)
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
    return f"📌 {label} {len(messages)}개 대화 · {first_day} {first_time} ~ {last}"

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

def attach_media_text(messages, media=None):
    """ 작업 함수가 미리 만든 사진 설명·음성 받아쓰기를 표식 뒤에 붙인 사본 (방 설정에서 끈 종류는 붙이지 않음) """
    allowed = lambda m: not media or media.get(m.get('media_type'), True)
    return [dict(m, message=f"{m['message']} — 내용: {m['media_text']}") if m.get('media_text') and allowed(m) else m for m in messages]

def pick_media(messages, media=None):
    """ 아직 설명이 없는(방금 올라와 처리 중이거나 실패한) 최근 사진·짧은 음성 몇 개 (시간순) """
    config = settings.MediaConfig
    pending = [m for m in messages if not m.get('media_text') and (not media or media.get(m.get('media_type'), True))]
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
# 방별 설정 (설정 페이지=텔레그램 미니앱에서 각 방 관리자가 바꿈)
# ===============================================================

SETTINGS_PARTITION = 'CHAT_SETTINGS'  # 방 설정 (대화 파티션에 두면 최신순 조회에 섞이므로 따로)
STATE_PARTITION = 'CHAT_STATE'        # 지난 요약 시각
_settings_cache = {}
_prompt_cache = {}

def default_chat_settings():
    c = settings.ChatConfig
    return {'tone': c.TONE[0], 'vocab': c.VOCAB[0], 'focus': c.FOCUS[0], 'detail_lines': c.DETAIL_LINES[0],
            'expanded': False, 'default_range': c.DEFAULT_RANGE[0], 'media': {k: True for k in c.MEDIA_KEYS},
            'excluded_ids': set(), 'version': 0}

def clean_chat_settings(raw):
    """ 저장값을 허용 목록으로 다시 검증 (모르는 값·깨진 값은 기본값) """
    c = settings.ChatConfig
    cfg = default_chat_settings()
    if not raw:
        return cfg
    for key, allowed in (('tone', c.TONE), ('vocab', c.VOCAB), ('focus', c.FOCUS), ('default_range', c.DEFAULT_RANGE)):
        if raw.get(key) in allowed:
            cfg[key] = raw[key]
    # 항목마다 따로 검증 (하나가 깨져도 제외 목록은 반드시 읽음)
    try:
        if int(raw.get('detail_lines', c.DETAIL_LINES[0])) in c.DETAIL_LINES:
            cfg['detail_lines'] = int(raw.get('detail_lines', c.DETAIL_LINES[0]))
    except (TypeError, ValueError):
        pass
    for x in (raw.get('excluded_ids') or set()):
        try:
            cfg['excluded_ids'].add(int(x))
        except (TypeError, ValueError):
            pass
    try:
        cfg['version'] = int(raw.get('version', 0))
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get('expanded'), bool):
        cfg['expanded'] = raw['expanded']
    media = raw.get('media') if isinstance(raw.get('media'), dict) else {}
    cfg['media'] = {k: media.get(k, True) is not False for k in c.MEDIA_KEYS}
    return cfg

def get_chat_settings(chat_id, fresh=False):
    """ 방 설정 (60초 캐시). 설정이 없거나 읽기 실패면 기본값 = 지금 동작 """
    key = str(chat_id)
    hit = _settings_cache.get(key)
    if hit and not fresh and hit[0] > time.time():
        return hit[1]
    try:
        item = table.get_item(Key={'chat_id': SETTINGS_PARTITION, 'timestamp': key}).get('Item')
    except Exception as e:
        print(f"Settings Read Error: {e}")
        item = None
    cfg = clean_chat_settings(item)
    _settings_cache[key] = (time.time() + settings.ChatConfig.CACHE_SECONDS, cfg)
    return cfg

def build_system_prompt(cfg):
    """ 기본 프롬프트(실측한 그대로) + 기본값이 아닌 설정만 <chat_style>로 덧붙임 (관리자가 쓴 글자는 넣지 않음) """
    c = settings.ChatConfig
    lines = []
    for key, defaults in (('tone', c.TONE), ('vocab', c.VOCAB), ('focus', c.FOCUS)):
        if cfg[key] != defaults[0]:
            lines.append(c.STYLE[key][cfg[key]])
    if cfg['detail_lines'] not in (c.DETAIL_LINES[0], 0):
        n = cfg['detail_lines']
        lines.append(f"- Detail block: at most {n} lines instead of 5 (the {n} most important subjects). List them most important first, "
                     f"not oldest first: the system keeps the first {n} and then sorts them by time.")
    base = settings.PromptConfig.SYSTEM_TEMPLATE
    if not lines:
        return base
    key = tuple(lines)
    if key not in _prompt_cache:
        style = "  <chat_style>\n    " + c.STYLE_HEADER + "\n    " + "\n    ".join(lines) + "\n  </chat_style>\n"
        end = base.rindex("</system_instruction>")
        _prompt_cache[key] = base[:end] + style + base[end:]
    return _prompt_cache[key]

def shape_output(text, cfg):
    """
    설정에 따라 모양만 바꿈 (시간순 정렬 전에 부름): 한 줄 결론만이면 접힌 상세를 빼고,
    줄 수를 바꾼 방은 앞에서부터 그 줄 수만 남김 (모델이 줄 수 지시를 자주 넘겨서, 중요한 순으로 쓰게 하고 코드로 자름)
    """
    n = cfg['detail_lines']
    if n == 0:
        return re.sub(r"\s*<blockquote expandable>.*?</blockquote>", "", text, flags=re.S)
    if n == settings.ChatConfig.DETAIL_LINES[0]:
        return text
    block = re.search(r"(<blockquote expandable>)(.*?)(</blockquote>)", text, re.S)
    if not block:
        return text
    lines = [l.strip() for l in block.group(2).strip().splitlines() if l.strip()]
    return text[:block.start()] + block.group(1) + "\n".join(lines[:n]) + block.group(3) + text[block.end():]

def get_last_summary_time(chat_id):
    try:
        item = table.get_item(Key={'chat_id': STATE_PARTITION, 'timestamp': str(chat_id)}).get('Item') or {}
        return item.get('last_summary_at')
    except Exception as e:
        print(f"State Read Error: {e}")
        return None

def save_summary_time(chat_id, when):
    try:
        table.put_item(Item={'chat_id': STATE_PARTITION, 'timestamp': str(chat_id), 'message_id': 0, 'last_summary_at': when})
    except Exception as e:
        print(f"State Save Error: {e}")

def set_opt_out(chat_id, user_id, on):
    """ 멤버 본인의 제외 켜고 끄기 (관리자 설정 저장과 부딪히지 않게 ADD/DELETE만) """
    table.update_item(
        Key={'chat_id': SETTINGS_PARTITION, 'timestamp': str(chat_id)},
        UpdateExpression=('ADD' if on else 'DELETE') + ' excluded_ids :u',
        ExpressionAttributeValues={':u': {int(user_id)}},
    )
    _settings_cache.pop(str(chat_id), None)

def delete_user_messages(chat_id, user_id):
    """ 그 멤버가 남긴 저장 메시지를 지움 (보낸 사람 번호가 기록된 것만: v1.8.0 이후 저장분) """
    from boto3.dynamodb.conditions import Attr
    deleted, start = 0, None
    with table.batch_writer() as batch:
        while True:
            kwargs = {'KeyConditionExpression': Key('chat_id').eq(str(chat_id)),
                      'FilterExpression': Attr('user_id').eq(int(user_id)), 'ProjectionExpression': 'chat_id, #ts',
                      'ExpressionAttributeNames': {'#ts': 'timestamp'}}
            if start:
                kwargs['ExclusiveStartKey'] = start
            page = table.query(**kwargs)
            for item in page.get('Items', []):
                batch.delete_item(Key={'chat_id': item['chat_id'], 'timestamp': item['timestamp']})
                deleted += 1
            start = page.get('LastEvaluatedKey')
            if not start:
                return deleted

def make_chat_token(chat_id, now=None):
    """ 설정 링크에 실을 방 토큰: "방번호_만료_서명" (startapp 허용 문자 A-Za-z0-9_- 만 씀) """
    expires = int((now or time.time()) + settings.ChatConfig.TOKEN_HOURS * 3600)
    body = f"{chat_id}_{expires}"
    sign = hmac.new(os.environ.get("TELEGRAM_TOKEN", "").encode(), b"chat-settings:" + body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}_{sign}"

def verify_chat_token(token, now=None):
    """ 방 토큰 확인. 맞으면 방 번호(int), 틀리거나 만료면 None """
    try:
        chat_id, expires, sign = token.rsplit("_", 2)
        body = f"{int(chat_id)}_{int(expires)}"
    except (AttributeError, ValueError):
        return None
    good = hmac.new(os.environ.get("TELEGRAM_TOKEN", "").encode(), b"chat-settings:" + body.encode(), hashlib.sha256).hexdigest()[:32]
    if not sign.isascii() or not hmac.compare_digest(sign, good) or int(expires) < (now or time.time()):
        return None
    return int(chat_id)

def verify_init_data(init_data, max_age=86400, now=None):
    """ 텔레그램 미니앱 initData 서명 확인 (secret = HMAC_SHA256(key="WebAppData", msg=봇 토큰)). 맞으면 user dict """
    from urllib.parse import parse_qsl
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = pairs.pop('hash', '')
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", os.environ.get("TELEGRAM_TOKEN", "").encode(), hashlib.sha256).digest()
    if not received or not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received):
        return None
    try:
        if (now or time.time()) - int(pairs.get('auth_date', 0)) > max_age:
            return None
        user = json.loads(pairs.get('user') or '{}')
    except ValueError:
        return None
    return user if user.get('id') else None

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
    """ /sum 명령어 처리 (메시지에 답장으로 치면 handle_reply_summary) """
    if update.effective_message.reply_to_message:
        await handle_reply_summary(update, context.bot)
        return

    chat_id = update.effective_chat.id
    # 숫자 파싱 (/sum 50)
    parts = update.effective_message.text.split()
    if len(parts) > 1 and parts[1].isdigit():
        await send_summary(context.bot, chat_id, min(int(parts[1]), MAX_LIMIT))  # 안전장치
        return

    # 숫자가 없으면 방 설정의 기본 범위 (지난 요약이 없으면 100개)
    default_range = get_chat_settings(chat_id)['default_range']
    if default_range == 'since_last':
        last = get_last_summary_time(chat_id)
        if last:
            await send_summary(context.bot, chat_id, MAX_LIMIT, since=last, since_kind='last')
            return
        default_range = '100'
    await send_summary(context.bot, chat_id, int(default_range))

async def handle_reply_summary(update, bot):
    """
    features:
        답장으로 친 /sum
        - 사진·음성에 답장 -> 자세한 설명·받아쓰기
        - 링크가 든 메시지에 답장 -> 그 링크만 자세히 (유튜브는 영상, 그 밖은 페이지 본문)
        - 그 밖의 메시지에 답장 -> 그 메시지부터 지금까지 요약
        자세히 읽기는 오래 걸릴 수 있어 작업 함수가 처리해 그 명령에 답장으로 보냄 (방 설정에서 끈 종류는 거절)
    """
    command = update.effective_message
    target = command.reply_to_message
    chat_id = update.effective_chat.id
    base = {'task': 'detail', 'chat_id': chat_id, 'reply_to': command.message_id}
    media_on = get_chat_settings(chat_id)['media']

    media = media_file(target)
    if media:
        name = '사진' if media['media_type'] == 'photo' else '음성'
        if not media_on.get(media['media_type'], True):
            await bot.send_message(chat_id=chat_id, text=f"이 방은 {name} 읽기가 꺼져 있습니다.")
            return
        if media['media_type'] == 'voice' and media.get('duration', 0) > settings.DetailConfig.VOICE_MAX_SECONDS:
            await bot.send_message(chat_id=chat_id, text="5분이 넘는 음성은 받아 적지 않습니다.")
            return
        await bot.send_message(chat_id=chat_id, text="🔎 자세히 보고 있어요...")
        invoke_worker({**base, 'kind': media['media_type'], 'file_id': media['file_id']})
        return

    links = [clean_url(u) for u in URL_PATTERN.findall(target.text or target.caption or "")]
    if links:
        kind = 'youtube' if youtube_ids(links[0]) else 'page'
        if not media_on.get('youtube' if kind == 'youtube' else 'link', True):
            await bot.send_message(chat_id=chat_id, text=f"이 방은 {'유튜브' if kind == 'youtube' else '링크'} 읽기가 꺼져 있습니다.")
            return
        await bot.send_message(chat_id=chat_id, text="🔎 링크를 자세히 읽고 있어요...")
        invoke_worker({**base, 'kind': kind, 'url': links[0]})
        return

    # 답장한 메시지 시각부터 (저장 시각은 보낸 시각보다 조금 늦으므로 2초 여유)
    KST = timezone(timedelta(hours=9))
    since = (target.date.astimezone(KST) - timedelta(seconds=2)).isoformat()
    await send_summary(bot, chat_id, MAX_LIMIT, since=since)

async def handle_more_button(update, bot):
    """ 요약 아래 [최근 N개로 다시] 버튼 처리 """
    query = update.callback_query
    await query.answer()  # 버튼의 로딩 표시를 끔

    data = query.data or ""
    if data.startswith("sum:") and data[4:].isdigit():
        await send_summary(bot, query.message.chat.id, min(int(data[4:]), MAX_LIMIT))

# since가 있을 때 머리줄·안내문: (범위 안 전부, 최대 개수에 걸려 최근만, 읽는 중 안내)
SINCE_LABELS = {
    'reply': ("답장한 지점부터", "답장한 지점 이후 최근", "답장한 메시지부터"),
    'last': ("지난 요약 이후", "지난 요약 이후 최근", "지난 요약 이후"),
}

async def send_summary(bot, chat_id, limit, since=None, since_kind='reply'):
    """ 최근 limit개(since가 있으면 그 시각 이후, 최대 limit개)를 방 설정대로 요약해 보냄 (명령어·버튼·답장이 함께 씀) """
    cfg = get_chat_settings(chat_id)
    labels = SINCE_LABELS[since_kind] if since else None
    #TODO: 나중에 입력중으로 변경(입력중 + 안내 메시지)
    waiting = f"🤖 {labels[2]} 대화를 읽고 있어요..." if since else f"🤖 최근 {limit}개의 대화 내용을 읽고 있어요..."
    await bot.send_message(chat_id=chat_id, text=waiting)

    # DB 조회 -> 중복 제거 -> 제외를 켠 멤버의 메시지 빼기 -> XML 변환
    chat_history = get_clean_chat_history(chat_id, limit, since=since)
    if cfg['excluded_ids']:
        chat_history = [m for m in chat_history if m.get('user_id') is None or int(m['user_id']) not in cfg['excluded_ids']]
    label = (labels[1] if len(chat_history) >= limit else labels[0]) if since else "최근"

    if not chat_history:
        await bot.send_message(chat_id=chat_id, text="요약할 대화 내용이 없습니다.")
        return

    # 링크 제목·설명과 사진·음성을 동시에 가져옴 (방 설정에서 끈 종류는 건너뜀, 실패해도 요약은 진행)
    media_on = cfg['media']
    links = recent_links(chat_history, settings.LinkConfig.LIMIT)
    titles, downloaded = await asyncio.gather(fetch_link_titles(links if media_on['link'] else []),
                                              download_media(bot, pick_media(chat_history, media_on)))
    if media_on['youtube']:
        titles = add_video_summaries(titles, links)  # 유튜브는 미리 만들어 둔 영상 요약도 덧붙임
    # 사진·음성은 미리 만든 설명을 붙이고, 아직 설명이 없는 것만 파일을 직접 첨부
    messages, media_parts = attach_media(attach_link_titles(attach_media_text(chat_history, media_on), titles), downloaded)
    xml_data = format_messages_to_xml(messages)

    # AI 호출
    try:
        # 방 설정(말투·어휘·초점·줄 수)이 기본값이 아니면 프롬프트 끝에 <chat_style>을 덧붙임
        prompt = build_system_prompt(cfg)
        config = generate_config if prompt is settings.PromptConfig.SYSTEM_TEMPLATE else generate_config.model_copy(update={'system_instruction': prompt})

        # AI에게 요약 생성 요청 (대화 XML 뒤에 번호 붙은 사진·음성)
        response = client.models.generate_content(
            model=settings.AIConfig.MODEL_NAME,
            contents=[xml_data, *media_parts],
            config=config,
        )

        log_model_verdict("summary", response)
        response_text = (response.text or "").strip()

        # 안전 필터 등으로 본문이 비어 오는 경우
        if not response_text:
            await bot.send_message(chat_id=chat_id, text="❌ 요약을 만들지 못했습니다. 잠시 뒤 다시 시도해 주세요.")
            return

        # 방 설정대로 줄 수를 맞춘 뒤(한 줄 결론만이면 상세를 뺌), 접힌 상세를 화제가 처음 나온 시간순으로 (최근이 아래)
        response_text = sort_details_by_time(shape_output(response_text, cfg), chat_history[-1]['timestamp'])
        body_html = to_telegram_html(response_text)
        if cfg['expanded']:
            body_html = body_html.replace("<blockquote expandable>", "<blockquote>")

        header = format_header(chat_history, label)
        reply_markup = more_buttons(limit)

        # 안전장치
        try:
            # HTML 모드로 전송
            await bot.send_message(
                chat_id=chat_id,
                text=f"{html.escape(header)}\n\n{body_html}",
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

        # '지난 요약 이후' 범위의 기준점
        save_summary_time(chat_id, datetime.now(timezone(timedelta(hours=9))).isoformat())

    except Exception as e:
        # AI 생성 실패 or 네트워크 오류 등 기타 치명적 에러
        await bot.send_message(chat_id=chat_id, text=f"❌ 시스템 오류: {e}")

async def handle_settings_command(update, context):
    """ /settings: 이 방의 설정 페이지(텔레그램 미니앱) 링크를 보냄 """
    chat = update.effective_chat
    if chat.type == 'private':
        await context.bot.send_message(chat_id=chat.id, text="설정은 요약 봇이 있는 단톡방에서 /settings 로 여세요.")
        return
    link = f"https://t.me/{context.bot.username}/{settings.ChatConfig.MINIAPP_SHORT_NAME}?startapp={make_chat_token(chat.id)}"
    await context.bot.send_message(
        chat_id=chat.id,
        text="⚙️ 이 방의 요약 설정입니다. 관리자는 말투·범위·읽기 설정을 바꿀 수 있고, 누구나 '내 메시지 제외'를 켤 수 있어요. (링크는 24시간 유효)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ 설정 열기", url=link)]]),
    )

async def handle_opt_out(update, context):
    """ /optout: 이 방에서 내 메시지를 저장·요약하지 않음 (이미 저장된 내 메시지도 지움) """
    chat, user = update.effective_chat, update.effective_user
    if chat.type == 'private' or not user:
        await context.bot.send_message(chat_id=chat.id, text="단톡방에서 쳐 주세요.")
        return
    set_opt_out(chat.id, user.id, True)
    deleted = delete_user_messages(chat.id, user.id)
    await context.bot.send_message(chat_id=chat.id, text=f"✅ 이제 이 방에서 내 메시지는 저장·요약하지 않아요. 저장돼 있던 {deleted}개도 지웠어요. 되돌리려면 /optin")

async def handle_opt_in(update, context):
    """ /optin: 내 메시지 제외를 끔 """
    chat, user = update.effective_chat, update.effective_user
    if chat.type == 'private' or not user:
        await context.bot.send_message(chat_id=chat.id, text="단톡방에서 쳐 주세요.")
        return
    set_opt_out(chat.id, user.id, False)
    await context.bot.send_message(chat_id=chat.id, text="✅ 다시 이 방의 요약에 내 메시지를 포함해요.")

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
        "- 메시지에 답장으로 <code>/sum</code>: 그 메시지부터 요약 (링크·사진·음성이면 그것만 자세히)\n"
        "- <code>/settings</code>: 이 방의 요약 설정 (관리자는 말투·범위·읽기 설정, 누구나 내 메시지 제외)\n"
        "- <code>/optout</code>·<code>/optin</code>: 내 메시지를 이 방에서 저장·요약하지 않기 / 되돌리기\n"
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
    "/settings": handle_settings_command,
    "/optout": handle_opt_out,
    "/optin": handle_opt_in,
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
                cfg = get_chat_settings(chat_id)
                sender_id = user.id if user else (message.sender_chat.id if message.sender_chat else None)
                # 본인이 제외를 켠 멤버의 메시지는 아예 저장하지 않음
                if sender_id is not None and sender_id in cfg['excluded_ids']:
                    return {"statusCode": 200, "body": "Opted out"}
                item = save_message_to_db(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    # 익명 관리자·채널 글은 from_user가 없으므로 보낸 채팅 이름으로 대신함
                    user_name=user.first_name if user else (message.sender_chat.title if message.sender_chat else "알 수 없음"),
                    text=text,
                    message_date=message.date,
                    media=media_file(message),
                    user_id=sender_id
                )
                # 사진·음성은 설명을, 유튜브 링크는 영상 요약을 미리 맡김 (작업 함수가 비동기로 처리, 방 설정에서 끈 종류는 건너뜀)
                request_media_description(item, cfg['media'])
                request_video_summaries(text, cfg['media'])

        except Exception as e:
            print(f"Error: {e}")
            return {"statusCode": 200, "body": str(e)}

    return {"statusCode": 200, "body": "OK"}

# ===============================================================
# 설정 페이지 (텔레그램 미니앱): GET /settings = 페이지, /settings/api = 읽기·저장
# ===============================================================

SETTINGS_PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_page.html")

def json_response(status, data):
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            "body": json.dumps(data, ensure_ascii=False)}

def validate_changes(raw):
    """ 관리자가 보낸 설정을 엄격히 검증 (모르는 키·허용 밖 값이 하나라도 있으면 None) """
    c = settings.ChatConfig
    allowed = {'tone': c.TONE, 'vocab': c.VOCAB, 'focus': c.FOCUS, 'detail_lines': c.DETAIL_LINES, 'default_range': c.DEFAULT_RANGE}
    if not isinstance(raw, dict) or not raw or set(raw) - set(allowed) - {'expanded', 'media'}:
        return None
    clean = {}
    for key, value in raw.items():
        if key == 'expanded':
            if not isinstance(value, bool):
                return None
        elif key == 'media':
            if not isinstance(value, dict) or set(value) != set(c.MEDIA_KEYS) or not all(isinstance(v, bool) for v in value.values()):
                return None
        elif isinstance(value, bool) or value not in allowed[key]:
            return None
        clean[key] = value
    return clean

def save_chat_settings(chat_id, changes, version, user_id):
    """ 관리자 설정 저장. 판 번호(version)가 그대로일 때만 (동시 수정 충돌 방지). 제외 목록은 건드리지 않음 """
    names, values, sets = {'#version': 'version'}, {':v': int(version), ':n': int(version) + 1, ':by': int(user_id),
                                                    ':at': datetime.now(timezone.utc).isoformat(), ':zero': 0}, []
    for i, (key, value) in enumerate(changes.items()):
        names[f'#k{i}'] = key
        values[f':v{i}'] = value
        sets.append(f'#k{i} = :v{i}')
    table.update_item(
        Key={'chat_id': SETTINGS_PARTITION, 'timestamp': str(chat_id)},
        UpdateExpression='SET ' + ', '.join(sets + ['#version = :n', 'updated_by = :by', 'updated_at = :at', 'message_id = :zero']),
        ConditionExpression='attribute_not_exists(#version) OR #version = :v',
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    _settings_cache.pop(str(chat_id), None)

def public_settings(cfg):
    """ 페이지에 보낼 설정 (제외한 멤버는 누구인지 빼고 인원만) """
    view = {k: cfg[k] for k in ('tone', 'vocab', 'focus', 'detail_lines', 'expanded', 'default_range', 'media', 'version')}
    view['excluded_count'] = len(cfg['excluded_ids'])
    return view

async def settings_api(event):
    """ 설정 읽기·저장. 텔레그램 서명(initData)으로 누구인지, 방 토큰으로 어느 방인지, getChatMember로 권한 확인 """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization") or ""
    user = verify_init_data(auth[4:] if auth.startswith("tma ") else "")
    if not user:
        return json_response(401, {"error": "텔레그램 안에서 열어 주세요."})
    method = event.get("httpMethod", "GET")
    body = {}
    if method == "POST":
        if len(event.get("body") or "") > 4096:
            return json_response(400, {"error": "요청이 너무 커요."})
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError:
            return json_response(400, {"error": "잘못된 요청이에요."})
    token = body.get("t") if method == "POST" else (event.get("queryStringParameters") or {}).get("t")
    chat_id = verify_chat_token(token or "")
    if chat_id is None:
        return json_response(403, {"error": "링크가 만료됐어요. 단톡방에서 /settings 를 다시 쳐 주세요."})

    async with telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN")) as bot:
        try:
            member = await bot.get_chat_member(chat_id, user['id'])
            chat = await bot.get_chat(chat_id)
        except telegram.error.TelegramError:
            return json_response(403, {"error": "이 방의 멤버가 아니에요."})
    is_admin = member.status in ("creator", "administrator")
    if not is_admin and member.status not in ("member", "restricted"):
        return json_response(403, {"error": "이 방의 멤버가 아니에요."})

    deleted = None
    if method == "POST":
        action = body.get("action")
        if action == "optout" and isinstance(body.get("value"), bool):
            set_opt_out(chat_id, user['id'], body["value"])
            deleted = delete_user_messages(chat_id, user['id']) if body["value"] else 0
        elif action == "save":
            if not is_admin:
                return json_response(403, {"error": "설정은 관리자만 바꿀 수 있어요."})
            changes = validate_changes(body.get("settings"))
            if changes is None or not isinstance(body.get("version"), int):
                return json_response(400, {"error": "허용되지 않는 값이 있어요."})
            try:
                save_chat_settings(chat_id, changes, body["version"], user['id'])
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    return json_response(409, {"error": "다른 관리자가 먼저 바꿨어요. 새로 불러올게요."})
                raise
        else:
            return json_response(400, {"error": "잘못된 요청이에요."})

    cfg = get_chat_settings(chat_id, fresh=True)
    return json_response(200, {"chat_title": chat.title, "is_admin": is_admin, "settings": public_settings(cfg),
                               "me": {"opted_out": int(user['id']) in cfg['excluded_ids']}, "deleted": deleted})

def handle_settings_http(event):
    """ GET /settings -> 설정 페이지 HTML, /settings/api -> 읽기·저장 """
    if event.get("resource") == "/settings":
        with open(SETTINGS_PAGE_PATH, encoding="utf-8") as f:
            page = f.read()
        return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, "body": page}
    try:
        return asyncio.run(settings_api(event))
    except Exception as e:
        print(f"Settings API Error: {e}")
        return json_response(500, {"error": "잠시 뒤 다시 시도해 주세요."})

def webhook_secret():
    """ 웹훅 비밀 토큰 = 봇 토큰에서 유도 (따로 보관할 비밀이 늘지 않음, setWebhook의 secret_token과 같은 값) """
    return hmac.new(os.environ.get("TELEGRAM_TOKEN", "").encode(), b"telegram-webhook-secret", hashlib.sha256).hexdigest()

def is_from_telegram(event):
    """ 텔레그램이 보낸 요청인지: X-Telegram-Bot-Api-Secret-Token 머리값 확인 """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return hmac.compare_digest(headers.get("x-telegram-bot-api-secret-token") or "", webhook_secret())

def lambda_handler(event, context):
    # 설정 페이지·API는 미니앱 서명(initData)으로 따로 확인
    if (event.get("resource") or "").startswith("/settings"):
        return handle_settings_http(event)
    # 텔레그램이 아닌 곳에서 온 요청(위조 업데이트)은 처리하지 않음
    if not is_from_telegram(event):
        print("Rejected: missing or wrong webhook secret")
        return {"statusCode": 403, "body": "Forbidden"}
    return asyncio.run(main_logic(event, context))