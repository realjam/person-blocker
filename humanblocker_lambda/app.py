import json
import os
import uuid

import boto3
import requests
from chalice import Chalice

app = Chalice(app_name='humanblocker_lambda')
sqs = boto3.client('sqs')
s3 = boto3.client('s3')


# To verify the webhook endpoint
@app.route('/', methods=['GET'])
def verify_callback():
    request = app.current_request
    print(request.query_params)
    if request.query_params is not None and 'hub.challenge' in request.query_params:
        print(json.dumps(request.query_params))
        return request.query_params['hub.challenge']
    else:
        print(request.to_dict())
        return "Hello World"


# Get First name of sender
def get_name(user_id: int, ACCESS_TOKEN):
    url = "https://graph.facebook.com/v2.10/{0}?access_token={1}".format(user_id, ACCESS_TOKEN)
    try:
        response = requests.get(url).json()
        return "Hey {0}, ".format(response['first_name'])
    except:
        return ""


# Reply text message to user(FB: receiver_id)
def send_to_fb(receiver_id: int, text, ACCESS_TOKEN):
    FB_URL = 'https://graph.facebook.com/v2.10/me/messages?access_token=' + ACCESS_TOKEN
    payload = {
        "recipient": {
            "id": str(receiver_id)
        },
        "message": {
            "text": text[:640]  # FB has the limitation of 640 char in a single message
        }
    }
    if len(text) > 0:
        resp = requests.post(FB_URL, json=payload)
        print(resp.content)
        if len(text) > 640:
            send_to_fb(receiver_id, text[640:], ACCESS_TOKEN)


def handle_message(messagings: list, access_token):
    for messaging in messagings:
        print(messaging)

        sender_id = messaging['sender']['id']
        sender_name = get_name(sender_id, access_token)
        # Handle 'Get Started' greetings
        if 'postback' in messaging:
            msg = "{0}\n Let's get start and send some image to hide human from that it".format(sender_name)
            send_to_fb(sender_id, msg, access_token)
            return

        message = messaging['message']

        if 'text' in message:
            msgs = "{0}\n I am not designed to handle text: {1}\nSend me an image to hide human from that it. ".format(
                sender_name, message['text'])

            send_to_fb(sender_id, msgs, access_token)
        elif 'attachments' in message:
            for attachment in message['attachments']:
                if 'type' in attachment and attachment['type'] == 'image':
                    url = attachment['payload']['url']
                    filename = url.split('/')[-1]
                    filename = filename[:filename.index('?')]

                    if filename.endswith("gif"):
                        send_to_fb(sender_id, "{0} GIF not supported.".format(sender_name), access_token)
                        return
                    # Create SQS queue or get SQS queue URL
                    queue_url = sqs.create_queue(
                        QueueName='tensor-flow.fifo',
                        Attributes={
                            'FifoQueue': 'true'
                        }
                    )['QueueUrl']
                    key = "fb/raw/{0}/{1}".format(sender_id, filename).replace("_", "")
                    #  Save image to S3
                    response = s3.put_object(
                        ACL='public-read',
                        Body=requests.get(url).content,
                        Bucket='tensorjam',
                        Key=key
                    )
                    print("Uploaded to S3: {0}".format(response))
                    #  Model running on EC2. Send the s3 URL to SQS
                    response = sqs.send_message(
                        QueueUrl=queue_url,
                        MessageBody="https://s3.amazonaws.com/tensorjam/{0}".format(key),
                        MessageGroupId='MsgGrpId',
                        MessageDeduplicationId=str(uuid.uuid4())
                    )
                    text = "Black Mirror on work. It has been batched. Have Patience. Thanks"
                    print(response)
                    # Send some dummy text to user to wait
                    send_to_fb(sender_id, text, access_token)
                else:
                    send_to_fb(sender_id, "Not a valid Image", access_token)
        else:
            send_to_fb(sender_id, "I don't understand your request", access_token)


@app.route('/', methods=['POST'])
def handle_fb_webhook():
    request = app.current_request

    try:
        #  Configure access_token environment var in config.json file
        ACCESS_TOKEN = os.environ['ACCESS_TOKEN']

        request_json = request.json_body
        print("Processing\n {0}".format(json.dumps(request_json)))

        if request_json is not None and 'entry' in request_json:
            entries = request_json['entry']
            for entry in entries:
                if 'messaging' in entry:
                    handle_message(entry['messaging'], ACCESS_TOKEN)
        else:
            print("No entry found to process")
    except Exception as e:
        print("ERROR: {0}".format(e))
        return "ERROR: {0}\n".format(e)
    return request.json_body
