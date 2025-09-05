from django.urls import path, include

urlpatterns = [
    path("api/", include("alerts.urls")),
    path("", include("alerts.urls")),  # SPA frontend same app
]
