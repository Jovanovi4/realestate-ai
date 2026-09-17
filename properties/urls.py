from django.contrib.auth import views as auth_views
from django.urls import path

from .ai_views import generate_description
from .forms import PhoneAuthenticationForm
from .views import (
    PropertyCreateView,
    PropertyDetailView,
    PropertyDeleteView,
    PropertyImageUploadView,
    PropertyListView,
    PropertyUpdateView,
    PublicLandingView,
    RealtorProfileUpdateView,
    LeadListView,
    LeadDetailView,
    LeadDeleteView,
    register,
)

urlpatterns = [
    path("register/", register, name="register"),
    path("login/", auth_views.LoginView.as_view(authentication_form=PhoneAuthenticationForm), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("profile/", RealtorProfileUpdateView.as_view(), name="edit_profile"),
    path("leads/", LeadListView.as_view(), name="lead_list"),
    path("leads/<int:pk>/", LeadDetailView.as_view(), name="lead_detail"),
    path("leads/<int:pk>/delete/", LeadDeleteView.as_view(), name="lead_delete"),
    path("", PropertyListView.as_view(), name="property_list"),
    path("landing/<slug:slug>/", PublicLandingView.as_view(), name="public_landing"),
    path("create/", PropertyCreateView.as_view(), name="create_property"),
    path("<int:pk>/edit/", PropertyUpdateView.as_view(), name="edit_property"),
    path("<int:pk>/delete/", PropertyDeleteView.as_view(), name="delete_property"),
    path("<int:pk>/", PropertyDetailView.as_view(), name="property_detail"),
    path("<int:pk>/generate/", generate_description, name="generate_description"),
    path("<int:pk>/images/", PropertyImageUploadView.as_view(), name="upload_images"),
]
