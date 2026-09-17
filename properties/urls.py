from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from .ai_views import AIAssistantView, AIContentDeleteView, AIContentEditView, generate_description
from .forms import AccountPasswordChangeForm, PhoneAuthenticationForm
from .views import (
    AvitoExportView,
    PropertyCreateView,
    PropertyDetailView,
    PropertyDeleteView,
    PropertyImageUploadView,
    PropertyImagePrimaryView,
    PropertyImageDeleteView,
    PropertyImageReorderView,
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
    path("avito/export/", AvitoExportView.as_view(), name="avito_export"),
    path(
        "profile/password/",
        auth_views.PasswordChangeView.as_view(
            form_class=AccountPasswordChangeForm,
            template_name="registration/password_change_form.html",
            success_url=reverse_lazy("edit_profile"),
        ),
        name="password_change",
    ),
    path("leads/", LeadListView.as_view(), name="lead_list"),
    path("leads/<int:pk>/", LeadDetailView.as_view(), name="lead_detail"),
    path("leads/<int:pk>/delete/", LeadDeleteView.as_view(), name="lead_delete"),
    path("", PropertyListView.as_view(), name="property_list"),
    path("landing/<slug:slug>/", PublicLandingView.as_view(), name="public_landing"),
    path("create/", PropertyCreateView.as_view(), name="create_property"),
    path("<int:pk>/edit/", PropertyUpdateView.as_view(), name="edit_property"),
    path("<int:pk>/delete/", PropertyDeleteView.as_view(), name="delete_property"),
    path("<int:pk>/ai/", AIAssistantView.as_view(), name="ai_assistant"),
    path("<int:pk>/", PropertyDetailView.as_view(), name="property_detail"),
    path("<int:pk>/generate/", generate_description, name="generate_description"),
    path("ai/content/<int:pk>/", AIContentEditView.as_view(), name="ai_content_edit"),
    path("ai/content/<int:pk>/delete/", AIContentDeleteView.as_view(), name="ai_content_delete"),
    path("<int:pk>/images/", PropertyImageUploadView.as_view(), name="upload_images"),
    path("<int:pk>/images/reorder/", PropertyImageReorderView.as_view(), name="reorder_images"),
    path("<int:pk>/images/<int:image_pk>/primary/", PropertyImagePrimaryView.as_view(), name="set_primary_image"),
    path("<int:pk>/images/<int:image_pk>/delete/", PropertyImageDeleteView.as_view(), name="delete_property_image"),
]
