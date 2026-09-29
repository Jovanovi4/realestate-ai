from io import BytesIO
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .admin import PropertyAdmin, RealtorProfileAdmin
from .forms import PropertyForm, RealtorProfileForm, SafeImageField
from .models import Property, PropertyImage, RealtorProfile
from .services.image_upload_service import prepare_image_upload


def image_upload(format="JPEG", name=None, size=(20, 20), mode="RGB", suffix=b"", exif=None):
    buffer = BytesIO()
    image = Image.new(mode, size, (255, 255, 255, 0) if mode == "RGBA" else "white")
    image.save(buffer, format=format, **({"exif": exif} if exif else {}))
    extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[format]
    return SimpleUploadedFile(name or f"image.{extension}", buffer.getvalue() + suffix, content_type="image/jpeg")


class ImageValidationTests(SimpleTestCase):
    def test_corrupt_file_is_rejected_even_with_image_mime_type(self):
        with self.assertRaisesMessage(ValidationError, "не является изображением"):
            prepare_image_upload(SimpleUploadedFile("photo.jpg", b"<script>alert(1)</script>", content_type="image/jpeg"))

    def test_html_svg_and_disguised_extension_are_rejected(self):
        for name in ("image.html", "image.svg", "image.php", "image.png"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                prepare_image_upload(image_upload(name=name))

    @override_settings(PROPERTY_IMAGE_MAX_MB=1)
    def test_file_size_limit_is_checked_before_decoding(self):
        with self.assertRaisesMessage(ValidationError, "1 МБ"):
            prepare_image_upload(image_upload(suffix=b"x" * 1024 * 1024))

    @override_settings(IMAGE_UPLOAD_MAX_PIXELS=399)
    def test_pixel_limit_is_enforced(self):
        with self.assertRaises(ValidationError) as error:
            prepare_image_upload(image_upload())
        self.assertEqual(error.exception.code, "image_too_large")

    def test_decompression_bomb_warning_is_an_error_not_a_fallback(self):
        with patch("properties.services.image_upload_service.Image.open", side_effect=Image.DecompressionBombWarning):
            with self.assertRaises(ValidationError) as error:
                prepare_image_upload(image_upload())
        self.assertEqual(error.exception.code, "image_too_large")

    def test_truncated_jpeg_is_rejected(self):
        source = image_upload()
        with self.assertRaises(ValidationError):
            prepare_image_upload(SimpleUploadedFile("truncated.jpg", source.read()[:-20]))

    def test_animation_is_rejected(self):
        buffer = BytesIO()
        Image.new("RGB", (20, 20), "white").save(
            buffer, format="WEBP", save_all=True, append_images=[Image.new("RGB", (20, 20), "black")], duration=100,
        )
        with self.assertRaisesMessage(ValidationError, "Анимированные"):
            prepare_image_upload(SimpleUploadedFile("animated.webp", buffer.getvalue()))

    def test_reencoding_strips_exif_and_appended_html_and_randomizes_name(self):
        exif = Image.Exif()
        exif[270] = "private metadata"
        result = prepare_image_upload(image_upload(name="unsafe-name.jpg", exif=exif, suffix=b"<script>payload</script>"))
        content = result.read()
        self.assertNotIn(b"private metadata", content)
        self.assertNotIn(b"payload", content)
        self.assertRegex(result.name, r"^property-[a-f0-9]{32}\.jpg$")
        with Image.open(BytesIO(content)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertFalse(decoded.getexif())

    def test_all_allowed_formats_are_decoded_and_photo_is_resized(self):
        for format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(format=format):
                result = prepare_image_upload(image_upload(format=format, size=(2100, 20)))
                with Image.open(result) as decoded:
                    self.assertEqual(decoded.format, "JPEG")
                    self.assertEqual(decoded.width, 2000)

    def test_logo_keeps_transparency_without_original_filename(self):
        result = prepare_image_upload(image_upload(format="PNG", mode="RGBA"), purpose="logo")
        with Image.open(result) as decoded:
            self.assertEqual(decoded.format, "PNG")
            self.assertEqual(decoded.getpixel((0, 0))[3], 0)
        self.assertRegex(result.name, r"^logo-[a-f0-9]{32}\.png$")

    @override_settings(PROFILE_IMAGE_MAX_MB=1)
    def test_avatar_and_logo_have_their_own_size_limit(self):
        for purpose in ("profile", "logo"):
            with self.subTest(purpose=purpose), self.assertRaisesMessage(ValidationError, "1 МБ"):
                prepare_image_upload(image_upload(suffix=b"x" * 1024 * 1024), purpose=purpose)

    def test_admin_upload_fields_use_the_same_validation(self):
        request = RequestFactory().get("/admin/")
        for admin_class, model, name in ((PropertyAdmin, Property, "landing_logo"), (RealtorProfileAdmin, RealtorProfile, "photo")):
            field = admin_class(model, admin.site).formfield_for_dbfield(model._meta.get_field(name), request)
            self.assertIsInstance(field, SafeImageField)
            with self.assertRaises(ValidationError):
                field.clean(SimpleUploadedFile("fake.jpg", b"not an image"))


class ImageUploadEndpointTests(TestCase):
    def setUp(self):
        media_dir = TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.media_root = Path(media_dir.name)
        self.user = User.objects.create_user("upload-owner", password="password")
        self.property = Property.objects.create(owner=self.user, title="Квартира")
        self.client.force_login(self.user)
        self.url = reverse("upload_images", args=[self.property.pk])

    def test_invalid_batch_saves_neither_photos_nor_files(self):
        response = self.client.post(self.url, {"images": [image_upload(), SimpleUploadedFile("fake.jpg", b"bad")]})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'class="app-form-error-summary"', status_code=400)
        self.assertEqual(self.property.images.count(), 0)
        self.assertEqual(list(self.media_root.rglob("*")), [])

    @override_settings(PROPERTY_IMAGES_PER_UPLOAD=2)
    def test_batch_count_limit_rejects_every_file(self):
        response = self.client.post(self.url, {"images": [image_upload() for _ in range(3)]})
        self.assertContains(response, "не более 2 фотографий", status_code=400)
        self.assertEqual(self.property.images.count(), 0)

    @override_settings(PROPERTY_IMAGES_MAX_COUNT=2)
    def test_total_limit_includes_existing_images(self):
        PropertyImage.objects.create(property=self.property, image="properties/existing.jpg", is_primary=True)
        response = self.client.post(self.url, {"images": [image_upload(), image_upload()]})
        self.assertContains(response, "не более 2 фотографий", status_code=400)
        self.assertEqual(self.property.images.count(), 1)

    @override_settings(PROPERTY_IMAGES_MAX_COUNT=2, PROPERTY_IMAGES_PER_UPLOAD=2)
    def test_exact_limits_are_allowed_and_only_one_image_is_primary(self):
        response = self.client.post(self.url, {"images": [image_upload(), image_upload(format="PNG")]})
        self.assertRedirects(response, reverse("property_detail", args=[self.property.pk]))
        self.assertEqual(self.property.images.count(), 2)
        self.assertEqual(self.property.images.filter(is_primary=True).count(), 1)
        self.assertEqual(list(self.property.images.order_by("order").values_list("order", flat=True)), [0, 1])
        for image in self.property.images.all():
            with Image.open(image.image.path) as decoded:
                self.assertEqual(decoded.format, "JPEG")

    def test_second_upload_does_not_reuse_zero_order(self):
        self.client.post(self.url, {"images": image_upload()})
        self.client.post(self.url, {"images": image_upload()})
        self.assertEqual(list(self.property.images.order_by("order").values_list("order", flat=True)), [0, 1])

    def test_retry_with_same_key_does_not_save_photo_twice(self):
        key = str(uuid.uuid4())
        first = self.client.post(self.url, {"images": image_upload(), "request_key": key})
        second = self.client.post(self.url, {"images": image_upload(), "request_key": key})
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(self.property.images.count(), 1)

    def test_reusing_upload_key_for_different_file_is_rejected(self):
        key = str(uuid.uuid4())
        self.client.post(self.url, {"images": image_upload(), "request_key": key})
        response = self.client.post(self.url, {"images": image_upload(name="another.jpg"), "request_key": key})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.property.images.count(), 1)

    def test_another_users_property_cannot_receive_uploads(self):
        other = User.objects.create_user("other", password="password")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, {"images": image_upload()}).status_code, 404)
        self.assertEqual(self.property.images.count(), 0)

    def test_profile_rejects_bad_upload_and_preserves_existing_photo(self):
        profile = RealtorProfile.objects.create(user=self.user, photo="realtors/existing.jpg")
        response = self.client.post(reverse("edit_profile"), {"photo": SimpleUploadedFile("fake.jpg", b"bad")})
        self.assertEqual(response.status_code, 200)
        self.assertIn("photo", response.context["form"].errors)
        profile.refresh_from_db()
        self.assertEqual(profile.photo.name, "realtors/existing.jpg")
        self.assertEqual(list(self.media_root.rglob("*")), [])

    def test_profile_valid_upload_is_reencoded(self):
        response = self.client.post(reverse("edit_profile"), {"photo": image_upload(format="WEBP")})
        self.assertRedirects(response, reverse("edit_profile"))
        profile = RealtorProfile.objects.get(user=self.user)
        self.assertRegex(profile.photo.name, r"^realtors/profile-[a-f0-9]{32}\.jpg$")

    def test_property_form_rejects_fake_logo(self):
        form = PropertyForm(files={"landing_logo": SimpleUploadedFile("fake.png", b"bad")}, data={"title": "Квартира"}, instance=self.property)
        self.assertFalse(form.is_valid())
        self.assertIn("landing_logo", form.errors)

    def test_unchanged_photo_and_explicit_logo_clear_still_work(self):
        profile = RealtorProfile.objects.create(user=self.user, photo="realtors/existing.jpg")
        form = RealtorProfileForm(data={}, instance=profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["photo"].name, "realtors/existing.jpg")
        self.property.landing_logo = "landing_logos/existing.png"
        form = PropertyForm(files={}, data={"landing_logo-clear": "on"}, instance=self.property)
        form.is_valid()
        self.assertIs(form.cleaned_data["landing_logo"], False)
