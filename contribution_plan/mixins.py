from django.conf import settings
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import (
    ImproperlyConfigured,
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError,
)
from django.db import models
from django.db.models import Q
from graphql import ResolveInfo
from uuid import UUID

from core.models import HistoryModelManager


class GenericPlanManager(HistoryModelManager):
    def filter(self, *args, **kwargs):
        keys = [x for x in kwargs if "itemsvc" in x]
        for key in keys:
            new_key = key.replace("itemsvc", self.model.model_prefix)
            kwargs[new_key] = kwargs.pop(key)
        return super(GenericPlanManager, self).filter(*args, **kwargs)


class GenericPlanQuerysetMixin:

    @classmethod
    def get_queryset(cls, queryset, user):
        queryset = cls.filter_queryset(queryset)
        if isinstance(user, ResolveInfo):
            user = user.context.user
        if settings.ROW_SECURITY and user.is_anonymous:
            return queryset.filter(id=-1)
        if settings.ROW_SECURITY:
            pass
        return queryset


def _supported_payment_plan_content_types():
    product_model = apps.get_model('product', 'Product')
    supported = {
        'product': ContentType.objects.get_for_model(product_model),
    }
    if apps.is_installed('social_protection'):
        benefit_plan_model = apps.get_model('social_protection', 'BenefitPlan')
        supported['benefitplan'] = ContentType.objects.get_for_model(
            benefit_plan_model
        )
    return supported


def social_protection_benefit_plan_content_type():
    return _supported_payment_plan_content_types().get('benefitplan')


def benefit_plan_access_control_enabled():
    """Read the optional Social Protection flag without importing it at load time."""
    if not apps.is_installed('social_protection'):
        return False
    try:
        from social_protection.apps import SocialProtectionConfig
    except ImportError:
        return False
    return bool(
        getattr(
            SocialProtectionConfig,
            'enable_benefit_plan_access_control',
            False,
        )
    )


def _shared_benefit_plan_access_helpers():
    """Load Track 01 lazily and fail closed whenever enforcement is enabled."""
    try:
        from social_protection.benefit_plan_access import (
            get_accessible_benefit_plan_ids,
            require_benefit_plan_access,
        )
    except ImportError as exc:
        raise ImproperlyConfigured(
            "Benefit Plan access control is enabled, but the shared Social "
            "Protection authorization service is unavailable."
        ) from exc
    return get_accessible_benefit_plan_ids, require_benefit_plan_access


def _validated_content_type(content_type=None, model_name=None, default_product=False):
    supported = _supported_payment_plan_content_types()
    if model_name is not None:
        content_type = supported.get(str(model_name).lower())
    elif content_type is not None and not isinstance(content_type, ContentType):
        try:
            content_type = ContentType.objects.get(pk=content_type)
        except (ContentType.DoesNotExist, TypeError, ValueError):
            content_type = None
    elif content_type is None and default_product:
        content_type = supported['product']

    if content_type not in supported.values():
        raise ValidationError(
            "Payment Plans may reference only Product or Social Protection Benefit Plan objects."
        )
    return content_type


def resolve_payment_plan_reference(user, data, current=None, default_product=False):
    """Validate and normalize a Payment Plan's polymorphic target."""
    if not benefit_plan_access_control_enabled():
        return _resolve_legacy_payment_plan_reference(
            data,
            current=current,
            default_product=default_product,
        )

    model_name = data.get('benefit_plan_type__model')
    content_type = data.get('benefit_plan_type')
    if model_name is None and content_type is None and current is not None:
        content_type = current.benefit_plan_type
    content_type = _validated_content_type(
        content_type=content_type,
        model_name=model_name,
        default_product=default_product,
    )

    object_id = data.get('benefit_plan_id')
    if object_id is None and current is not None:
        object_id = current.benefit_plan_id
    if object_id in (None, ''):
        raise ValidationError("Payment Plan Benefit Plan ID is required.")

    benefit_plan_type = social_protection_benefit_plan_content_type()
    if content_type == benefit_plan_type:
        try:
            object_id = str(UUID(str(object_id)))
        except (TypeError, ValueError, AttributeError):
            raise ValidationError("Social Protection Benefit Plan ID must be a valid UUID.")
        _, require_benefit_plan_access = _shared_benefit_plan_access_helpers()
        require_benefit_plan_access(user, object_id)
    else:
        object_id = str(object_id)

    if not content_type.get_all_objects_for_this_type(pk=object_id).exists():
        raise ValidationError("Referenced Payment Plan target does not exist.")
    return content_type, object_id


def _resolve_legacy_payment_plan_reference(data, current=None, default_product=False):
    """Retain pre-Track-06 target handling while enforcement is disabled."""
    has_content_type = 'benefit_plan_type' in data
    has_object_id = 'benefit_plan_id' in data
    model_name = data.get('benefit_plan_type__model')

    content_type = (
        data.get('benefit_plan_type')
        if has_content_type
        else getattr(current, 'benefit_plan_type', None)
    )
    object_id = (
        data.get('benefit_plan_id')
        if has_object_id
        else getattr(current, 'benefit_plan_id', None)
    )

    if model_name:
        try:
            content_type = ContentType.objects.get(model=str(model_name).lower())
            content_type.get_object_for_this_type(pk=object_id)
        except (
            ContentType.DoesNotExist,
            ObjectDoesNotExist,
            TypeError,
            ValueError,
        ) as exc:
            raise ValidationError("Referenced Payment Plan target does not exist.") from exc

    return content_type, object_id


def require_payment_plan_access(user, payment_plan):
    if payment_plan is None:
        raise PermissionDenied("Payment Plan access is required.")
    if not benefit_plan_access_control_enabled():
        return
    benefit_plan_type = social_protection_benefit_plan_content_type()
    if benefit_plan_type is None:
        raise ImproperlyConfigured(
            "Benefit Plan access control is enabled without Social Protection."
        )
    if payment_plan.benefit_plan_type_id == benefit_plan_type.pk:
        try:
            benefit_plan_id = str(UUID(str(payment_plan.benefit_plan_id)))
        except (TypeError, ValueError, AttributeError):
            raise PermissionDenied("Payment Plan access is required.")
        _, require_benefit_plan_access = _shared_benefit_plan_access_helpers()
        require_benefit_plan_access(user, benefit_plan_id)


def filter_payment_plans_by_benefit_plan(queryset, user):
    """Scope Social Protection Payment Plans while retaining legacy Product plans."""
    if not benefit_plan_access_control_enabled():
        return queryset
    get_accessible_benefit_plan_ids, _ = _shared_benefit_plan_access_helpers()
    accessible_ids = get_accessible_benefit_plan_ids(user)
    if accessible_ids is None:
        return queryset
    normalized_ids = [str(plan_id) for plan_id in accessible_ids]
    benefit_plan_type = social_protection_benefit_plan_content_type()
    return queryset.filter(
        ~Q(benefit_plan_type_id=benefit_plan_type.pk)
        | Q(
            benefit_plan_type_id=benefit_plan_type.pk,
            benefit_plan_id__in=normalized_ids,
        )
    )
