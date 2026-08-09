"""Serializers for the stapel-docs API."""
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import PingResponse


class PingResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = PingResponse
