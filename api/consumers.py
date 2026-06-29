from __future__ import annotations

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class PipelineConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.lecture_id = self.scope['url_route']['kwargs']['lecture_id']
        self.group_name = f'lecture-pipeline-{self.lecture_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def pipeline_progress(self, event):
        await self.send_json(event['payload'])
