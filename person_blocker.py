import os
import time

import boto3
import botocore
import imageio
import numpy as np
import requests

import model as modellib
import utils
from classes import get_class_names, InferenceConfig

s3 = boto3.client('s3')

# Read FB Token from environment variable
ACCESS_TOKEN = os.environ['ACCESS_TOKEN']

#  Get bucket name. Bucket must be in US N.virginia region otherwise facebook complain if they found '-' in url
BUCKET = os.environ['BUCKET_NAME']


def create_noisy_color(image, color):
    color_mask = np.full(shape=(image.shape[0], image.shape[1], 3),
                         fill_value=color)

    noise = np.random.normal(0, 25, (image.shape[0], image.shape[1]))
    noise = np.repeat(np.expand_dims(noise, axis=2), repeats=3, axis=2)
    mask_noise = np.clip(color_mask + noise, 0., 255.)
    return mask_noise


# Send Text message to Sender
def send_to_fb_text(receiver_id: int, text):
    FB_URL = 'https://graph.facebook.com/v2.10/me/messages?access_token=' + ACCESS_TOKEN
    payload = {
        "recipient": {
            "id": str(receiver_id)
        },
        "message": {
            "text": text[:640]
        }
    }

    if len(text) > 0:
        resp = requests.post(FB_URL, json=payload)
        print(resp.content)
        if len(text) > 640:
            send_to_fb_text(receiver_id, text[640:])


# Reply the generated image to sender
def send_to_fb(receiver_id, file_url):
    FB_URL = 'https://graph.facebook.com/v2.12/me/messages?access_token=' + ACCESS_TOKEN
    payload = {
        "recipient": {
            "id": str(receiver_id)
        },
        "message": {
            "attachment": {
                "type": "image",
                "payload": {
                    "is_reusable": False,
                    "url": file_url
                }
            }
        }
    }
    resp = requests.post(FB_URL, json=payload)
    print(resp.json())


def load_model():
    ROOT_DIR = os.getcwd()
    COCO_MODEL_PATH = os.path.join(ROOT_DIR, "mask_rcnn_coco.h5")

    MODEL_DIR = os.path.join(ROOT_DIR, "logs")  # Required to load model

    if not os.path.exists(COCO_MODEL_PATH):
        utils.download_trained_weights(COCO_MODEL_PATH)

    # Load model and config
    config = InferenceConfig()
    model = modellib.MaskRCNN(mode="inference",
                              model_dir=MODEL_DIR, config=config)
    model.load_weights(COCO_MODEL_PATH, by_name=True)

    return model


# Load model only for one time
model = load_model()


# Required to load model, but otherwise unused
def person_blocker(image_path, sender_id):
    image = imageio.imread(image_path)

    # Create masks for all objects
    results = model.detect([image], verbose=0)
    r = results[0]

    # Filter masks to only the selected objects
    objects = np.array('person')

    # Object IDs:
    if np.all(np.chararray.isnumeric(objects)):
        object_indices = objects.astype(int)
    # Types of objects:
    else:
        selected_class_ids = np.flatnonzero(np.in1d(get_class_names(),
                                                    objects))
        object_indices = np.flatnonzero(
            np.in1d(r['class_ids'], selected_class_ids))

    mask_selected = np.sum(r['masks'][:, :, object_indices], axis=2)

    # Replace object masks with white noise
    mask_color = (255, 255, 255)
    image_masked = image.copy()
    noisy_color = create_noisy_color(image, mask_color)
    image_masked[mask_selected > 0] = noisy_color[mask_selected > 0]

    dest = "/tmp/{0}".format(image_path.split("/")[-1])
    imageio.imwrite(dest, image_masked)
    key = "fb/processed/{0}".format(sender_id + dest)

    # Upload image to S3 as the FB need URL to send image in messages
    response = s3.put_object(
        ACL='public-read',
        Body=open(dest, 'rb').read(),
        Bucket=BUCKET,
        ContentType="image/png",
        Key=key,
    )

    file_url = "https://s3.amazonaws.com/{0}/{1}".format(BUCKET, key)
    print(file_url)
    send_to_fb(sender_id, file_url)


if __name__ == "__main__":
    sqs = boto3.client('sqs')
    # Create SQS queue same name as used in lambda or get url of queue
    queue_url = sqs.create_queue(
        QueueName='tensor-flow.fifo',
        Attributes={
            'FifoQueue': 'true'
        }
    )['QueueUrl']

    while True:
        try:
            sender_id = None
            # Poll the SQS queue
            messages = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=10
            )
            print(messages)
            print(time.time())
            if 'Messages' in messages:
                for message in messages['Messages']:
                    print(message['Body'])
                    #  Delete the message from queue
                    sqs.delete_message(
                        QueueUrl=queue_url,
                        ReceiptHandle=message['ReceiptHandle']
                    )
                    # Extract sender id from image URL
                    image_path = message['Body']
                    sender_id = image_path.split("/")[-2]
                    person_blocker(image_path, sender_id)
        except (botocore.exceptions.ClientError, Exception) as e:
            print("error", e)
            if sender_id is not None and len(sender_id) > 5:
                send_to_fb_text(sender_id, "Got an error:{0}".format(e))
