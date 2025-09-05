from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),   # <-- serve the HTML page
    path("watchlists/", views.get_watchlists, name="get_watchlists"),
    path("refresh-sheet/", views.refresh_sheet, name="refresh_sheet"),
    path("refresh-all-prices/", views.refresh_all_prices, name="refresh_all_prices"),
    path("refresh-tab/<str:tab_name>/", views.refresh_tab_prices, name="refresh_tab_prices"),
]
