from __future__ import annotations

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db.models import Q

from api.models import Lecture, User


class PipelineConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.lecture_id = self.scope['url_route']['kwargs']['lecture_id']
        user = self.scope.get('user')
        if not user or not user.is_authenticated or not user.is_active:
            await self.close(code=4401)
            return
        if not await self._user_can_access_lecture(user):
            await self.close(code=4403)
            return
        self.group_name = f'lecture-pipeline-{self.lecture_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def pipeline_progress(self, event):
        await self.send_json(event['payload'])

    @database_sync_to_async
    def _user_can_access_lecture(self, user) -> bool:
        queryset = Lecture.objects.filter(pk=self.lecture_id, is_active=True)
        if user.is_superuser:
            return queryset.exists()
        if user.organization_id is None:
            return False
        if user.role == User.Role.ADMIN:
            return queryset.filter(classroom__organization_id=user.organization_id).exists()
        if user.role == User.Role.PROFESSOR:
            return queryset.filter(
                classroom__organization_id=user.organization_id,
            ).filter(
                Q(classroom__professor_id=user.id)
                | Q(classroom__class_teacher_id=user.id)
                | Q(classroom__subject_teacher_assignments__teacher_id=user.id)
            ).distinct().exists()
        if user.role == User.Role.STUDENT:
            return queryset.filter(
                classroom__organization_id=user.organization_id,
                classroom__students__id=user.id,
            ).exists()
        return False
