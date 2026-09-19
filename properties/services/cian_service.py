from decimal import Decimal
from xml.etree import ElementTree as ET


class CianExportService:
    """Builds a CIAN Feed v2 XML file for manual import."""

    TYPE_MAPPING = {
        "apartment": "flatSale",
        "house": "houseSale",
        "cottage": "houseSale",
        "villa": "houseSale",
        "land": "landSale",
    }
    RENT_TYPE_MAPPING = {
        "apartment": "flatRent",
        "house": "houseRent",
        "cottage": "houseRent",
        "villa": "houseRent",
    }
    CURRENCY_MAPPING = {"RUB": "rur", "USD": "usd", "EUR": "eur"}

    @classmethod
    def validation_errors(cls, property, profile):
        errors = []
        if property.status != "published":
            errors.append("объект не опубликован")
        if not property.price:
            errors.append("не указана цена")
        if not property.address:
            errors.append("не указан адрес")
        if not property.description or len(property.description.strip()) < 15:
            errors.append("полное описание должно содержать не менее 15 символов")
        if not property.images.exists():
            errors.append("не добавлена хотя бы одна фотография")
        if not profile or not profile.phone:
            errors.append("в личном кабинете не указан телефон риелтора")
        type_mapping = cls.RENT_TYPE_MAPPING if property.deal_type == "rent" else cls.TYPE_MAPPING
        if property.property_type not in type_mapping:
            errors.append("тип недвижимости пока не поддерживается в экспорте ЦИАН")
        if property.property_type != "land" and not property.area:
            errors.append("не указана площадь объекта")
        if property.property_type == "land" and not property.land_area:
            errors.append("не указана площадь участка")
        return errors

    @classmethod
    def get_exportable(cls, properties, profile):
        valid, invalid = [], []
        for property in properties:
            errors = cls.validation_errors(property, profile)
            (invalid if errors else valid).append((property, errors) if errors else property)
        return valid, invalid

    @classmethod
    def build_xml(cls, properties, profile, base_url):
        root = ET.Element("Feed")
        cls._add(root, "Feed_Version", "2")
        for property in properties:
            obj = ET.SubElement(root, "Object")
            type_mapping = cls.RENT_TYPE_MAPPING if property.deal_type == "rent" else cls.TYPE_MAPPING
            cls._add(obj, "Category", type_mapping[property.property_type])
            cls._add(obj, "ExternalId", f"realestate-ai-{property.pk}")
            cls._add(obj, "Address", property.address)
            cls._add(obj, "Description", property.description.strip())
            cls._add(obj, "DealType", "rent" if property.deal_type == "rent" else "sale")
            bargain_terms = ET.SubElement(obj, "BargainTerms")
            cls._add(bargain_terms, "Price", str(int(Decimal(property.price))))
            cls._add(bargain_terms, "Currency", cls.CURRENCY_MAPPING.get(property.currency, "rur"))
            ET.SubElement(obj, "Phones").append(ET.Element("PhoneSchema"))
            phone = obj.find("Phones/PhoneSchema")
            cls._add(phone, "CountryCode", "7")
            cls._add(phone, "Number", "".join(char for char in profile.phone if char.isdigit())[-10:])
            photos = ET.SubElement(obj, "Photos")
            for image in property.images.all():
                photo = ET.SubElement(photos, "PhotoSchema")
                cls._add(photo, "FullUrl", f"{base_url}{image.image.url}")
            if property.area:
                cls._add(obj, "TotalArea", str(property.area))
            if property.rooms:
                cls._add(obj, "RoomsCount", str(property.rooms))
            if property.floor:
                cls._add(obj, "FloorNumber", str(property.floor))
            if property.floors_total:
                cls._add(obj, "FloorsCount", str(property.floors_total))
            if property.land_area:
                cls._add(obj, "LandArea", str(property.land_area))
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _add(parent, tag, value):
        element = ET.SubElement(parent, tag)
        element.text = str(value)
