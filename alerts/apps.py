from django.apps import AppConfig


class AlertsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'alerts'

    def ready(self):    
        # Start scheduler when Django is ready
        from .tasks import start_scheduler
        start_scheduler()

