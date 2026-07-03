from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOrgMember, HasActiveSubscription
from apps.common.mixins import OrgQuerySetMixin
from apps.training.models import (
    BusinessProfile,
    ScenarioTemplate,
    Script,
    TrainingSession,
    SessionResult,
)
from apps.training.serializers import (
    BusinessProfileSerializer,
    ScenarioTemplateSerializer,
    ScriptSerializer,
    TrainingSessionSerializer,
    TrainingSessionListSerializer,
    SessionResultSerializer,
    SubmitTurnSerializer,
)
from apps.training.services.session_runner import start_session, process_turn, complete_session


class BusinessProfileViewSet(OrgQuerySetMixin, viewsets.ModelViewSet):
    queryset = BusinessProfile.objects.all()
    serializer_class = BusinessProfileSerializer
    permission_classes = [IsAuthenticated, IsOrgMember]

    def perform_create(self, serializer):
        serializer.save(org=self.request.org)


class ScenarioTemplateViewSet(OrgQuerySetMixin, viewsets.ModelViewSet):
    queryset = ScenarioTemplate.objects.all()
    serializer_class = ScenarioTemplateSerializer
    permission_classes = [IsAuthenticated, IsOrgMember]

    def perform_create(self, serializer):
        serializer.save(org=self.request.org)


class ScriptViewSet(OrgQuerySetMixin, viewsets.ModelViewSet):
    queryset = Script.objects.all()
    serializer_class = ScriptSerializer
    permission_classes = [IsAuthenticated, IsOrgMember]

    def perform_create(self, serializer):
        serializer.save(org=self.request.org)


class TrainingSessionViewSet(OrgQuerySetMixin, viewsets.ModelViewSet):
    queryset = TrainingSession.objects.select_related(
        'business_profile', 'scenario_template', 'script'
    )
    permission_classes = [IsAuthenticated, IsOrgMember, HasActiveSubscription]
    http_method_names = ['get', 'post', 'delete']

    def get_serializer_class(self):
        if self.action == 'list':
            return TrainingSessionListSerializer
        return TrainingSessionSerializer

    def perform_create(self, serializer):
        serializer.save(org=self.request.org)

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        session = self.get_object()
        if session.status != TrainingSession.Status.CREATED:
            return Response(
                {'detail': 'Session has already been started.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        turn = start_session(session)
        session.refresh_from_db()
        return Response(TrainingSessionSerializer(session).data)

    @action(detail=True, methods=['post'])
    def turn(self, request, pk=None):
        session = self.get_object()
        if session.status != TrainingSession.Status.RUNNING:
            return Response(
                {'detail': 'Session is not running.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = SubmitTurnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ai_turn = process_turn(session, serializer.validated_data['text'])
        session.refresh_from_db()
        return Response(TrainingSessionSerializer(session).data)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        session = self.get_object()
        if session.status != TrainingSession.Status.RUNNING:
            return Response(
                {'detail': 'Session is not running.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        complete_session(session)
        session.refresh_from_db()
        return Response(TrainingSessionSerializer(session).data)

    @action(detail=True, methods=['get'])
    def result(self, request, pk=None):
        session = self.get_object()
        try:
            result = session.result
            return Response(SessionResultSerializer(result).data)
        except SessionResult.DoesNotExist:
            return Response(
                {'detail': 'Result not available yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
