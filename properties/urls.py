from django.contrib.auth import views as auth_views
from django.urls import path

from .ai_views import generate_description
from .views import (
    PropertyCreateView,
    PropertyDetailView,
    PropertyImageUploadView,
    PropertyListView,
    PropertyUpdateView,
    register,
)

urlpatterns = [
    path("register/", register, name="register"),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", PropertyListView.as_view(), name="property_list"),
    path("create/", PropertyCreateView.as_view(), name="create_property"),
    path("<int:pk>/edit/", PropertyUpdateView.as_view(), name="edit_property"),
    path("<int:pk>/", PropertyDetailView.as_view(), name="property_detail"),
    path("<int:pk>/generate/", generate_description, name="generate_description"),
    path("<int:pk>/images/", PropertyImageUploadView.as_view(), name="upload_images"),
]
