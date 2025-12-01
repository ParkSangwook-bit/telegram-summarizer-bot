import json
import os
import asyncio
import time
from datetime import datetime, timedelta
import telegram
import google.generativeai as genai
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import settings # 설정파일

# ===============================================================
# 초기화
# ===============================================================

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(settings.AWSConfig.DYNAMO_TABLE)

if settings.AIConfig.API_KEY:
    genai.configure(api_key=settings.AIConfig.API_KEY)
    model = genai.GenerativeModel(settings.AIConfig.MODEL_NAME)
else:
    model = None


# ===============================================================
# 핵심 로직
# ===============================================================

def is_duplicate_request(update_id):
    """
    args:
        update_id (int): 텔레그램 업데이트 ID
    features:
        중복 요청을 확인하고, 중복 요청이 발생하면 True를 반환합니다.
    """
    
    try:
        # 중복 요청 확인 및 기록(24시간 TTL)
        ttl_seconds = int(time.time()) + (24 * 60 * 60)
        table.put_item(
            Item={
                'chat_id': 'SYSTEM_PROCESSED_UPDATES', 
                'timestamp': str(update_id),           
                'ttl': ttl_seconds
            },
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
        메시지를 DynamoDB에 저장합니다.
    """
    try:
        if isinstance(message_date, (int, float)):
            timestamp = datetime.fromtimestamp(message_date).isoformat()
        else:
            timestamp = datetime.now().isoformat()

        # TTL: 7일
        ttl_seconds = int(time.time()) + (7 * 24 * 60 * 60)

        item = {
            'chat_id': str(chat_id),
            'timestamp': timestamp,
            'message_id': message_id, # 중복 제거의 Key
            'user_name': user_name,
            'message': text,
            'ttl': ttl_seconds
        }
        table.put_item(Item=item)

    except Exception as e:
        print(f"DB Save Error: {e}")

def get_recent_messages(chat_id, limit=100):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
        limit (int): 최근 메시지 수
    features:
        DynamoDB에서 최근 메시지를 가져옵니다.
    """

    def get_clean_chat_history(chat_id, limit=100):
        '''
        args:
            chat_id (int): 텔레그램 채팅 ID
            limit (int): 최근 메시지 수
        features:

        '''
        # DB에서 limit보다 여유있게 가져옴
        raw_items = get_recent_messages(chat_id, limit = limit*1.5)

        # 시간순 정렬(과거->현재): 수정된 메시지가 딕셔너리를 덮어쓸 수 있도록
        raw_items = raw_items.sort(key = lambda x: x['timestamp'])

        # 딕셔너리를 이용한 중복제거.message)id가 같으면 나중에 들어온 텍스트 정보로 내용 덮어씌워짐.
        deduplicated_items = {item['message_id']: item for item in raw_items}

        # 다시 리스트로 변환
        clean_messages = list(deduplicated_items.values())

        # 기존의 순서인 최신순으로 정렬 후 반환
        return clean_messages[-limit:]
        

    try:
        response = table.query(
            KeyConditionExpression=Key('chat_id').eq(str(chat_id)),
            ScanIndexForward=False,
            Limit=limit
        )
        items = response.get('Items', [])
        return items[::-1] 
    except Exception as e:
        print(f"DB Query Error: {e}")
        return []

async def generate_summary(messages):
    """
    args:
        messages (list): 메시지 리스트
    features:
        메시지를 요약합니다.
    """
    if not model:
        return "오류: Gemini API 키가 설정되지 않았습니다."
    if not messages:
        return "요약할 최근 대화 내용이 없습니다."

    formatted_chat = ""
    for msg in messages:
        time_str = msg['timestamp'].split('T')[1][:5] 
        formatted_chat += f"[{time_str}] {msg['user_name']}: {msg['message']}\n"

    prompt = f"""
    당신은 텔레그램 그룹 채팅방의 대화 내용을 요약하는 건조하고 정확한 봇입니다.
    아래 대화 내용을 바탕으로 핵심 주제와 중요한 일정을 요약하세요.

    [제약 사항]
    1. **제목, 인사말, 맺음말을 절대 포함하지 마세요.**
    2. 오직 요약된 내용만 바로 출력하세요.
    3. 핵심 주제는 글머리 기호(-)로 나열하세요.
    4. 날짜, 시간, 장소 등 약속 정보가 있다면 '✨ 중요 일정' 섹션에 따로 명시하세요. 없으면 생략하세요.
    5. 어조는 간결한 '해요체'로 작성하세요.

    [대화 내용 시작]
    {formatted_chat}
    [대화 내용 끝]

    [요약 결과]
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI 처리 중 오류가 발생했습니다: {e}"


# ===============================================================
# 메인 핸들러
# ===============================================================

async def main_logic(event, context):
    """
    args:
        event (dict): AWS Lambda 이벤트
        context (object): AWS Lambda 컨텍스트
    features:
        중복 요청 방지.
        입력된 메시지가 명령어인지 일반 메시지인지 검사하는 라우팅 로직 포함.

    """
    # 봇 객체 생성 (아직 연결 안 됨)
    bot = telegram.Bot(token=TELEGRAM_TOKEN)

    #! async with를 사용하여 네트워크 연결 수명 관리
    async with bot: # async with bot이라는 것은 bot을 사용할 때만 연결을 유지하고, 사용이 끝나면 연결을 종료하는 역할. 
        try:
            body = json.loads(event.get("body", "{}"))  # json을 딕셔너리화
            update = telegram.Update.de_json(body, bot) # de_json은 각자 속성에 맞는 인스턴스를 생성하고, Update라는 하나의 클래스로 만듦

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
            user_name = user.first_name if user else "Unknown"

            if user and user.is_bot:
                return {"statusCode": 200, "body": "Bot message ignored"}

            # --- 라우팅 로직 ---
            
            if text.startswith("/summary"):
                try:
                    #! 검토: 메시지 전송중 상태로 보이게 할지(5~7초 까지밖에 안되므로, 전송중 상태 표시 -> 지연시 안내문 발송 등으로 UX개선)
                    await bot.send_message(chat_id=chat_id, text="🤖 잠시만요, 대화 내용을 읽고 있어요...")
                    
                    chat_history = get_recent_messages(chat_id, limit=100)
                    summary_result = await generate_summary(chat_history)
                    
                    await bot.send_message(chat_id=chat_id, text=summary_result, parse_mode='Markdown')
                    
                except Exception as process_error:
                    error_msg = f"❌ 요약 실패: 처리 중 문제가 발생했습니다.\n({str(process_error)})"
                    # 에러 메시지 전송 시도 (실패할 수도 있음 :') )
                    try:
                        await bot.send_message(chat_id=chat_id, text=error_msg)
                    except:
                        pass
                    print(f"Process Error: {process_error}")

            else:
                # 일반 대화 저장
                save_message_to_db(
                    chat_id=chat_id,
                    user_name=user_name,
                    text=text,
                    message_date=message.date
                )

        except Exception as e:
            print(f"Critical Handler Error: {e}")
            return {"statusCode": 200, "body": str(e)}

    return {"statusCode": 200, "body": "OK"}


def lambda_handler(event, context):
    """
    args:
        event (dict): AWS Lambda 이벤트
        context (object): AWS Lambda 컨텍스트
    features:
        AWS Lambda 핸들러입니다.
    """
    return asyncio.run(main_logic(event, context))


#=============================================================================================================
#=============================================================================================================
#=============================================================================================================

import json
import asyncio
import time
from datetime import datetime
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update # telegram ui용
import google.generativeai as genai
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
    genai.configure(api_key=settings.AIConfig.API_KEY)
    model = genai.GenerativeModel(settings.AIConfig.MODEL_NAME)
else:
    model = None

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
    try:
        if isinstance(message_date, (int, float)):
            timestamp = datetime.fromtimestamp(message_date).isoformat()
        else:
            timestamp = datetime.now().isoformat()

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
        토큰 절약을 위한 XML 변환
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
            
        user = msg['user_name']
        text = msg['message']
        xml += f'<l u="{user}" t="{time_str}">{text}</l>\n'
        
    xml += "</logs>"

    return xml

# ===============================================================
# 핸들러 함수 (Command Handlers)
# ===============================================================

async def handle_summary(update, context):
    """ /summary 명령어 처리 """
    chat_id = update.effective_chat.id
    text = update.effective_message.text
    
    # 숫자 파싱 (/summary 50)
    limit = 100
    parts = text.split()
    if len(parts) > 1 and parts[1].isdigit():
        limit = int(parts[1])
        # 안전장치
        if limit > 500: limit = 500
    
    #TODO: 나중에 입력중으로 변경(입력중 + 안내 메시지)
    await context.bot.send_message(chat_id=chat_id, text="🤖 잠시만요, 대화 내용을 읽고 있어요...")
    
    # DB 조회 -> 중복 제거 -> XML 변환
    chat_history = get_clean_chat_history(chat_id, limit)
    
    if not chat_history:
        await context.bot.send_message(chat_id=chat_id, text="요약할 대화 내용이 없습니다.")
        return

    xml_data = format_messages_to_xml(chat_history)
    
    # AI 호출
    try:
        final_input_prompt = f"{settings.PromptConfig.SYSTEM_TEMPLATE}\n{xml_data}"
        
        response = model.generate_content(
            final_input_prompt,
            generation_config=settings.AIConfig.GENERATION_CONFIG,
            safety_settings=settings.AIConfig.SAFETY_SETTINGS
        )
        await context.bot.send_message(chat_id=chat_id, text=response.text, parse_mode='Markdown')
        
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 요약 실패: {e}")

async def handle_about(update, context):
    """ /about 명령어: 봇 정보와 버튼 출력 """
    chat_id = update.effective_chat.id

    about_text = (
        f"🤖 **Telegram Summary Bot** ({settings.BOT_VERSION})\n\n"
        "바쁜 당신을 위해 대화 내용을 놓치지 않도록 \n"
        "**AI(Gemini)**가 핵심만 쏙쏙 요약해 드립니다.\n\n"
        "**현재 AI모델**: {settings.AIConfig.MODEL_NAME}\n"
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

    await context.bot.send_message(
        chat_id=chat_id,
        text=about_text,
        parse_mode='Markdown',
        reply_markup=reply_markup,
        disable_web_page_preview=True # 링크 미리보기 끄기
    )

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
    "/summary": handle_summary,
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
            command_key = text.split()[0] if text.startswith("/") else None
            
            # 딕셔너리에 있는 명령어면 실행
            if command_key in COMMAND_HANDLERS:
                # duck typing
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