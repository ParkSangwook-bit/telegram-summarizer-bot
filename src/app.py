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

# ===============================================================
# 초기화
# ===============================================================

# 환경 변수로 설정. 이 환경 변수들은 AWS 클라우드 컴퓨터(Lambda)에 있음.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DYNAMO_TABLE_NAME = os.environ.get("DYNAMO_TABLE_NAME")

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(DYNAMO_TABLE_NAME)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
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

def save_message_to_db(chat_id, user_name, text, message_date):
    """
    args:
        chat_id (int): 텔레그램 채팅 ID
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

        # 메시지 저장 (7일 TTL)
        ttl_seconds = int(time.time()) + (7 * 24 * 60 * 60)

        item = {
            'chat_id': str(chat_id),
            'timestamp': timestamp,
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
        메인 핸들러입니다.
    """
    # 봇 객체 생성 (아직 연결 안 됨)
    bot = telegram.Bot(token=TELEGRAM_TOKEN)

    #! async with를 사용하여 네트워크 연결 수명 관리
    async with bot: # async with bot이라는 것은 bot을 사용할 때만 연결을 유지하고, 사용이 끝나면 연결을 종료하는 역할을 합니다. 
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