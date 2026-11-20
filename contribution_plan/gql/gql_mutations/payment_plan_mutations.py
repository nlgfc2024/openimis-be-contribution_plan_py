from core.gql.gql_mutations import DeleteInputType
from core.code_generation import generate_unique_year_code
from core.gql.gql_mutations.base_mutation import BaseMutation, BaseDeleteMutation, BaseReplaceMutation, \
    BaseHistoryModelCreateMutationMixin, BaseHistoryModelUpdateMutationMixin, \
    BaseHistoryModelDeleteMutationMixin, BaseHistoryModelReplaceMutationMixin
from contribution_plan.services import PaymentPlan as PaymentPlanService
from contribution_plan.gql.gql_mutations import PaymentPlanInputType, PaymentPlanUpdateInputType, \
    PaymentPlanReplaceInputType
from contribution_plan.apps import ContributionPlanConfig
from contribution_plan.models import PaymentPlan
from contribution_plan.mixins import (
    require_payment_plan_access,
    resolve_payment_plan_reference,
)
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _


class CreatePaymentPlanMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "PaymentPlanMutation"
    _mutation_module = "contribution_plan"
    _model = PaymentPlan

    @classmethod
    def create_object(cls, user, object_data):
        object_data = dict(object_data)
        if not object_data.get('code'):
            object_data['code'] = generate_unique_year_code(cls._model, {"is_deleted": False})
        content_type, benefit_plan_id = resolve_payment_plan_reference(
            user,
            object_data,
            default_product='benefit_plan_type__model' not in object_data,
        )
        object_data.pop('benefit_plan_type__model', None)
        object_data['benefit_plan_type'] = content_type
        object_data['benefit_plan_id'] = benefit_plan_id
        obj = cls._model(**object_data)
        obj.save(username=user.username)
        return obj

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                ContributionPlanConfig.gql_mutation_create_paymentplan_perms):
            raise ValidationError(_("mutation.authentication_required"))
        if data.get('code') and PaymentPlanService.check_unique_code(
            data['code'], user=user,
        ):
            raise ValidationError(_("mutation.payment_plan_code_duplicated"))

    class Input(PaymentPlanInputType):
        pass


class UpdatePaymentPlanMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "PaymentPlanMutation"
    _mutation_module = "contribution_plan"
    _model = PaymentPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                ContributionPlanConfig.gql_mutation_update_paymentplan_perms):
            raise ValidationError(_("mutation.authentication_required"))

        current = cls._model.objects.filter(id=data['id']).first()
        require_payment_plan_access(user, current)
        resolve_payment_plan_reference(user, data, current=current)

        if data.get('code') and PaymentPlanService.check_unique_code(
            data['code'], data['id'], user=user,
        ):
            raise ValidationError(_("mutation.payment_plan_code_duplicated"))

    @classmethod
    def _mutate(cls, user, **data):
        if "date_valid_to" not in data:
            data['date_valid_to'] = None
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')
        updated_object = cls._model.objects.filter(id=data['id']).first()
        require_payment_plan_access(user, updated_object)
        content_type, benefit_plan_id = resolve_payment_plan_reference(
            user, data, current=updated_object,
        )
        data.pop('benefit_plan_type__model', None)
        data['benefit_plan_type'] = content_type
        data['benefit_plan_id'] = benefit_plan_id
        [setattr(updated_object, key, data[key]) for key in data]
        cls.update_object(user=user, object_to_update=updated_object)

    class Input(PaymentPlanUpdateInputType):
        pass


class DeletePaymentPlanMutation(BaseHistoryModelDeleteMutationMixin, BaseDeleteMutation):
    _mutation_class = "PaymentPlanMutation"
    _mutation_module = "contribution_plan"
    _model = PaymentPlan

    @classmethod
    def async_mutate(cls, user, **data):
        # BaseDeleteMutation splits batches before invoking subclass validation.
        # Preflight the complete batch so an inaccessible item cannot leave an
        # earlier accessible item deleted.
        try:
            cls._validate_mutation(user, **data)
        except Exception as exc:
            return [{
                "message": "Failed to process {} mutation".format(
                    cls._mutation_class
                ),
                "detail": str(exc),
            }]
        return super().async_mutate(user, **data)

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                ContributionPlanConfig.gql_mutation_delete_paymentplan_perms):
            raise ValidationError(_("mutation.authentication_required"))
        ids = data.get('uuids') or data.get('ids') or []
        single_id = data.get('uuid') or data.get('id')
        if single_id:
            ids = [single_id]
        payment_plans = {
            str(payment_plan.pk): payment_plan
            for payment_plan in cls._model.objects.filter(pk__in=ids)
        }
        for payment_plan_id in ids:
            payment_plan = payment_plans.get(str(payment_plan_id))
            if payment_plan is None:
                raise PermissionDenied(_("unauthorized"))
            require_payment_plan_access(user, payment_plan)

    class Input(DeleteInputType):
        pass


class ReplacePaymentPlanMutation(BaseHistoryModelReplaceMutationMixin, BaseReplaceMutation):
    _mutation_class = "PaymentPlanMutation"
    _mutation_module = "contribution_plan"
    _model = PaymentPlan

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                ContributionPlanConfig.gql_mutation_replace_paymentplan_perms):
            raise ValidationError(_("mutation.authentication_required"))
        current = cls._model.objects.filter(id=data['uuid']).first()
        require_payment_plan_access(user, current)
        resolve_payment_plan_reference(user, data, current=current)

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        current = cls._model.objects.filter(id=data['uuid']).first()
        require_payment_plan_access(user, current)
        content_type, benefit_plan_id = resolve_payment_plan_reference(
            user, data, current=current,
        )
        data.pop('benefit_plan_type__model', None)
        data['benefit_plan_type'] = content_type
        data['benefit_plan_id'] = benefit_plan_id
        current.replace_object(data=data, username=user.username)

    class Input(PaymentPlanReplaceInputType):
        pass
