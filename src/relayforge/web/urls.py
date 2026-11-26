from django.urls import path

from relayforge.web.api import api
from relayforge.web.studio_ui import asset, shell

urlpatterns = [
    path("", shell),
    path("app/", shell),
    path("app/<slug:view>", shell),
    path("static/relayforge_ui/<str:filename>", asset),
    path("api/v1/", api.urls),
]
