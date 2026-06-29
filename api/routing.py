from django.urls import path

from api.consumers import PipelineConsumer


websocket_urlpatterns = [
    path('ws/pipeline/<int:lecture_id>/', PipelineConsumer.as_asgi()),
]
