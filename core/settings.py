"""Django settings for core project."""

import os
from datetime import timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default

SECRET_KEY = 'django-insecure-1@8)v+x#o)#a1-zmffbt%om=l__0#hshwj$y94*@7yq=#r_9vn'

DEBUG = True

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'rest_framework',
    'django_celery_results',
    'rest_framework_simplejwt.token_blacklist',
    'api.apps.ApiConfig',
]

USE_AZURE_BLOB_STORAGE = env_bool('USE_AZURE_BLOB_STORAGE', False)
if USE_AZURE_BLOB_STORAGE:
    INSTALLED_APPS.append('storages')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'api.middleware.SecurityHeadersMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


STATIC_URL = 'static/'
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
        'OPTIONS': {
            'location': MEDIA_ROOT,
            'base_url': MEDIA_URL,
        },
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}

AZURE_STORAGE_ACCOUNT_NAME = os.getenv('AZURE_STORAGE_ACCOUNT_NAME', '')
AZURE_STORAGE_ACCOUNT_KEY = os.getenv('AZURE_STORAGE_ACCOUNT_KEY', '')
AZURE_STORAGE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING', '')
AZURE_STORAGE_CONTAINER_NAME = os.getenv('AZURE_STORAGE_CONTAINER_NAME', 'user-videos')
AZURE_ENDPOINT_SUFFIX = os.getenv('AZURE_ENDPOINT_SUFFIX', 'core.windows.net')
AZURE_CUSTOM_DOMAIN = os.getenv('AZURE_CUSTOM_DOMAIN', '').strip() or None
AZURE_LOCATION = os.getenv('AZURE_LOCATION', '').strip()
AZURE_OVERWRITE_FILES = env_bool('AZURE_OVERWRITE_FILES', False)
AZURE_URL_EXPIRATION_SECS = env_int('AZURE_URL_EXPIRATION_SECS', None)

if USE_AZURE_BLOB_STORAGE:
    azure_storage_options = {
        'account_name': AZURE_STORAGE_ACCOUNT_NAME,
        'account_key': AZURE_STORAGE_ACCOUNT_KEY,
        'connection_string': AZURE_STORAGE_CONNECTION_STRING,
        'azure_container': AZURE_STORAGE_CONTAINER_NAME,
        'endpoint_suffix': AZURE_ENDPOINT_SUFFIX,
        'custom_domain': AZURE_CUSTOM_DOMAIN,
        'location': AZURE_LOCATION,
        'expiration_secs': AZURE_URL_EXPIRATION_SECS,
        'overwrite_files': AZURE_OVERWRITE_FILES,
    }
    STORAGES['default'] = {
        'BACKEND': 'storages.backends.azure_storage.AzureStorage',
        'OPTIONS': {k: v for k, v in azure_storage_options.items() if v not in {None, ''}},
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'api.User'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'default'
CELERY_TASK_TRACK_STARTED = True
CELERY_RESULT_EXTENDED = True
CELERY_RESULT_EXPIRES = 86400
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [os.getenv('CHANNEL_REDIS_URL', CELERY_BROKER_URL)],
        },
    },
}

# Azure AI Foundry / Cognitive Services
AZURE_OPENAI_ENDPOINT = os.getenv(
    'AZURE_OPENAI_ENDPOINT',
    'https://basic-agent-flow-zak-resource.services.ai.azure.com/openai/v1',
)
AZURE_OPENAI_API_KEY = os.getenv('AZURE_OPENAI_API_KEY', '')
AZURE_OPENAI_SCOPE = os.getenv('AZURE_OPENAI_SCOPE', 'https://ai.azure.com/.default')

AZURE_SPEECH_ENDPOINT = os.getenv(
    'AZURE_SPEECH_ENDPOINT',
    'https://basic-agent-flow-zak-resource.cognitiveservices.azure.com/',
)
AZURE_SPEECH_KEY = os.getenv('AZURE_SPEECH_KEY', '')

AZURE_TRANSLATOR_ENDPOINT = os.getenv('AZURE_TRANSLATOR_ENDPOINT', 'https://api.cognitive.microsofttranslator.com/')
AZURE_TRANSLATOR_KEY = os.getenv('AZURE_TRANSLATOR_KEY', '')
AZURE_TRANSLATOR_API_VERSION = os.getenv('AZURE_TRANSLATOR_API_VERSION', '2025-10-01-preview')

SYLLABUS_PROMPT_MAX_CHARS = int(os.getenv('SYLLABUS_PROMPT_MAX_CHARS', '200000'))
RAG_EMBEDDING_MODEL = os.getenv('RAG_EMBEDDING_MODEL', 'sentence-transformers/all-MiniLM-L6-v2')
RAG_CHUNK_SIZE = int(os.getenv('RAG_CHUNK_SIZE', '700'))
RAG_CHUNK_OVERLAP = int(os.getenv('RAG_CHUNK_OVERLAP', '120'))
VISUALIZER_CSP = os.getenv(
    'VISUALIZER_CSP',
    "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; base-uri 'none'",
)

# Brevo SMTP (email delivery)
BREVO_SMTP_HOST = os.getenv('BREVO_SMTP_HOST', 'smtp-relay.brevo.com')
BREVO_SMTP_PORT = int(os.getenv('BREVO_SMTP_PORT', '587'))
BREVO_SMTP_USER = os.getenv('BREVO_SMTP_USER', '')
BREVO_SMTP_KEY = os.getenv('BREVO_SMTP_KEY', '')
EMAIL_FROM_ADDRESS = os.getenv('EMAIL_FROM_ADDRESS', '')

# OTP test helper (DEBUG/local only)
FIXED_TEST_OTP = os.getenv('FIXED_TEST_OTP', '')

# Speech settings
CLOUD_TRANSCRIPTION_ONLY = os.getenv('CLOUD_TRANSCRIPTION_ONLY', 'true').lower() in {'1', 'true', 'yes', 'on'}
AZURE_SPEECH_LOCALES = os.getenv('AZURE_SPEECH_LOCALES', 'en-US,hi-IN')
