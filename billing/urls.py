from django.urls import path

from .views import BillingOverviewView


app_name = "billing"

urlpatterns = [
    path("", BillingOverviewView.as_view(), name="overview"),
]
