"""Validate untrusted images and store only newly encoded pixel data."""

import warnings
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


ALLOWED_FORMATS = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}, "WEBP": {".webp"}}


def prepare_image_upload(uploaded_file, *, purpose="property"):
    """Reject invalid uploads; never fall back to saving the original file."""
    max_mb = settings.PROPERTY_IMAGE_MAX_MB if purpose == "property" else settings.PROFILE_IMAGE_MAX_MB
    if uploaded_file.size > max_mb * 1024 * 1024:
        raise ValidationError(f"Размер изображения не должен превышать {max_mb} МБ.", code="image_too_large")
    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in {suffix for suffixes in ALLOWED_FORMATS.values() for suffix in suffixes}:
        raise ValidationError("Допустимы только изображения JPG, PNG и WEBP.", code="invalid_image_format")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            uploaded_file.seek(0)
            with Image.open(uploaded_file) as source:
                if source.format not in ALLOWED_FORMATS or extension not in ALLOWED_FORMATS[source.format]:
                    raise ValidationError("Формат изображения не соответствует расширению файла.", code="invalid_image_format")
                if source.width * source.height > settings.IMAGE_UPLOAD_MAX_PIXELS:
                    raise ValidationError(
                        f"Разрешение изображения не должно превышать {settings.IMAGE_UPLOAD_MAX_PIXELS // 1_000_000} мегапикселей.",
                        code="image_too_large",
                    )
                if getattr(source, "n_frames", 1) != 1:
                    raise ValidationError("Анимированные изображения не поддерживаются.", code="animated_image")
                source.verify()

            # verify() alone is insufficient: decode all pixels before saving.
            uploaded_file.seek(0)
            with Image.open(uploaded_file) as source:
                source.load()
                image = ImageOps.exif_transpose(source)
                edge = {"property": 2000, "profile": 800, "logo": 1200}[purpose]
                image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                if purpose == "logo":
                    image = image.convert("RGBA")
                    output_format, suffix = "PNG", "png"
                else:
                    rgba = image.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                    output_format, suffix = "JPEG", "jpg"
                # A fresh image carries no EXIF, comments, profiles or trailing data.
                clean_image = Image.new(image.mode, image.size)
                clean_image.paste(image)
                output = BytesIO()
                clean_image.save(output, format=output_format, **({"quality": 85, "optimize": True} if suffix == "jpg" else {}))
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ValidationError("Разрешение изображения слишком велико.", code="image_too_large") from error
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError, EOFError) as error:
        raise ValidationError("Файл повреждён или не является изображением JPG, PNG или WEBP.", code="invalid_image") from error
    finally:
        uploaded_file.seek(0)

    return ContentFile(output.getvalue(), name=f"{purpose}-{uuid4().hex}.{suffix}")
