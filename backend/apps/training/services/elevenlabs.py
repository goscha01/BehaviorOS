import os
import uuid
import requests
from django.conf import settings


def generate_speech(text, voice_id=None, model='eleven_monolingual_v1', stability=0.5, similarity_boost=0.75):
    """Generate speech audio from text using ElevenLabs API.

    Returns the relative URL path to the saved audio file.
    """
    voice_id = voice_id or settings.ELEVENLABS_VOICE_ID
    api_key = settings.ELEVENLABS_API_KEY

    if not api_key:
        return ''

    url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}'
    headers = {
        'Accept': 'audio/mpeg',
        'Content-Type': 'application/json',
        'xi-api-key': api_key,
    }
    payload = {
        'text': text,
        'model_id': model,
        'voice_settings': {
            'stability': stability,
            'similarity_boost': similarity_boost,
        },
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()

    audio_dir = os.path.join(settings.MEDIA_ROOT, 'audio')
    os.makedirs(audio_dir, exist_ok=True)

    filename = f'{uuid.uuid4()}.mp3'
    filepath = os.path.join(audio_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(response.content)

    return f'media/audio/{filename}'
