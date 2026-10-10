import os
import re
import html
import json
import asyncio
import time
from datetime import datetime, timedelta, timezone
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

def save_message_to_db(chat_id, message_id, user_name, text, message_date):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
        message_id (int): 텔레그램 메시지 ID
        user_name (str): 사용자 이름
        text (str): 메시지 내용
        message_date (int): 메시지 날짜
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
        table.put_item(Item=item)

    except Exception as e:
        print(f"DB Save Error: {e}")

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

# ===============================================================
# 핸들러 함수 (Command Handlers)
# ===============================================================

async def handle_summary(update, context):
    """ /sum 명령어 처리 """
    chat_id = update.effective_chat.id
    text = update.effective_message.text
    
    # 숫자 파싱 (/sum 50)
    limit = 100
    parts = text.split()
    if len(parts) > 1 and parts[1].isdigit():
        limit = int(parts[1])
        # 안전장치
        if limit > 500: limit = 500
    
    #TODO: 나중에 입력중으로 변경(입력중 + 안내 메시지)
    await context.bot.send_message(chat_id=chat_id, text=f"🤖 최근 {limit}개의 대화 내용을 읽고 있어요...")
    
    # DB 조회 -> 중복 제거 -> XML 변환
    chat_history = get_clean_chat_history(chat_id, limit)
    
    if not chat_history:
        await context.bot.send_message(chat_id=chat_id, text="요약할 대화 내용이 없습니다.")
        return

    xml_data = format_messages_to_xml(chat_history)
    
    # AI 호출
    try:
        
        # AI에게 요약 생성 요청
        response = client.models.generate_content(
            model=settings.AIConfig.MODEL_NAME,
            contents=xml_data,
            config=generate_config,
        )

        response_text = (response.text or "").strip()

        # 안전 필터 등으로 본문이 비어 오는 경우
        if not response_text:
            await context.bot.send_message(chat_id=chat_id, text="❌ 요약을 만들지 못했습니다. 잠시 뒤 다시 시도해 주세요.")
            return

        header = format_header(chat_history)

        # 안전장치
        try:
            # HTML 모드로 전송
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{html.escape(header)}\n\n{to_telegram_html(response_text)}",
                parse_mode='HTML'
            )
        except telegram.error.BadRequest:
            # HTML 파싱 에러 발생 시 -> 태그를 걷어낸 일반 텍스트로 재전송(전송 성공 보장)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{header}\n\n{strip_tags(response_text)}",
                parse_mode=None
            )
        
    except Exception as e:
        # AI 생성 실패 or 네트워크 오류 등 기타 치명적 에러
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 시스템 오류: {e}")

async def handle_about(update, context):
    """ /about 명령어: 봇 정보와 버튼 출력 """
    chat_id = update.effective_chat.id

    about_text = (
        f"🤖 **Telegram Summary Bot** ({settings.BOT_VERSION})\n\n"
        "바쁜 당신을 위해 대화 내용을 놓치지 않도록 \n"
        "**AI(Gemini)**가 핵심만 쏙쏙 요약해 드립니다.\n\n"
        f"**현재 AI모델**: {settings.AIConfig.MODEL_NAME}\n"
        "✨ **주요 기능**\n"
        "- `/summary`: 최근 대화 요약\n"
        "- 대화 자동 저장 및 만료 처리 (TTL)\n\n"
        f"📅 **마지막 업데이트:** {settings.LAST_UPDATE}"
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
            parse_mode='Markdown',
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

            if not update.effective_message or not update.effective_message.text:
                return {"statusCode": 200, "body": "No text message"}

            message = update.effective_message
            chat_id = message.chat.id
            text = message.text
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
                save_message_to_db(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    user_name=user.first_name,
                    text=text,
                    message_date=message.date
                )

        except Exception as e:
            print(f"Error: {e}")
            return {"statusCode": 200, "body": str(e)}

    return {"statusCode": 200, "body": "OK"}

def lambda_handler(event, context):
    return asyncio.run(main_logic(event, context))