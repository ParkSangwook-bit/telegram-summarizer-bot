import json

def lambda_handler(event, context):
    """
    AWS Lambda에서 실행될 메인 진입점.
    (API Gateway가 'event' 객체에 정보를 담아 호출)
    """

    # 디버깅을 위해 텔레그램에서 받은 이벤트를 로그로 출력.
    print("Event received:", json.dumps(event))

    # 텔레그램(API Gateway)에게 "정상 수신" 응답을 반환.
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hello from Lambda! Summarizer Bot is alive."
        }),
    }