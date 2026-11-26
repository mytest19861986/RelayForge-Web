from django.urls import path

from relayforge.web.api import api

urlpatterns = [path("api/v1/", api.urls)]
