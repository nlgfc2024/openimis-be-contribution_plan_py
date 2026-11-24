import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import graphene
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase, override_settings
from graphql_relay import to_global_id

from core.models import MutationLog
from core.test_helpers import (
    create_test_interactive_user,
    create_test_language,
    create_test_role,
)
from product.test_helpers import create_test_product
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BenefitPlan, UserBenefitPlanAccess

from contribution_plan.apps import ContributionPlanConfig
from contribution_plan.gql.gql_mutations.payment_plan_mutations import (
    CreatePaymentPlanMutation,
    DeletePaymentPlanMutation,
    ReplacePaymentPlanMutation,
    UpdatePaymentPlanMutation,
)
from contribution_plan.gql.gql_types import PaymentPlanGQLType
from contribution_plan.mixins import (
    filter_payment_plans_by_benefit_plan,
    require_payment_plan_access,
    resolve_payment_plan_reference,
)
from contribution_plan.models import PaymentPlan
from contribution_plan.schema import (
    Mutation as ContributionPlanMutation,
    Query as ContributionPlanQuery,
)
from contribution_plan.services import PaymentPlan as PaymentPlanService


class Query(ContributionPlanQuery, graphene.ObjectType):
    node = graphene.relay.Node.Field()


@override_settings(ROW_SECURITY=True, MODE="dev")
class PaymentPlanBenefitPlanAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_test_language()
        cls.admin = create_test_interactive_user(username="track06-admin")
        role = create_test_role(
            perm_names=[
                "gql_query_paymentplan_perms",
                "gql_mutation_create_paymentplan_perms",
                "gql_mutation_update_paymentplan_perms",
                "gql_mutation_delete_paymentplan_perms",
                "gql_mutation_replace_paymentplan_perms",
            ],
            name="Track 06 Payment Plan user",
        )
        cls.reader = create_test_interactive_user(
            username="track06-reader",
            roles=[role.id],
        )
        cls.plan_a = BenefitPlan(code="T06-A", name="Track 06 A")
        cls.plan_a.save(username=cls.admin.username)
        cls.plan_b = BenefitPlan(code="T06-B", name="Track 06 B")
        cls.plan_b.save(username=cls.admin.username)
        cls.benefit_plan_type = ContentType.objects.get_for_model(BenefitPlan)
        cls.product = create_test_product(f"T06{str(uuid4())[:4]}")
        cls.product_type = ContentType.objects.get_for_model(type(cls.product))
        cls.payment_plan_a = cls.payment_plan("T06-PP-A", cls.plan_a)
        cls.payment_plan_b = cls.payment_plan("T06-PP-B", cls.plan_b)
        cls.legacy_payment_plan = cls.payment_plan("T06-PP-LEG", cls.product)
        UserBenefitPlanAccess(
            user=cls.reader,
            benefit_plan=cls.plan_a,
        ).save(username=cls.admin.username)

    @classmethod
    def payment_plan(cls, code, target):
        payment_plan = PaymentPlan(
            code=code,
            name=code,
            calculation=uuid4(),
            benefit_plan=target,
            periodicity=1,
        )
        payment_plan.save(username=cls.admin.username)
        return payment_plan

    def setUp(self):
        original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        self.addCleanup(
            setattr,
            SocialProtectionConfig,
            "enable_benefit_plan_access_control",
            original_enabled,
        )
        self.addCleanup(
            setattr,
            SocialProtectionConfig,
            "gql_benefit_plan_all_access_perms",
            original_all,
        )

    def request(self):
        request = RequestFactory().get("/graphql")
        request.user = self.reader
        return request

    def permissions(self, user=None):
        user = user or self.reader
        all_plan_perms = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        return patch.object(
            type(user),
            "has_perms",
            side_effect=lambda perms: perms != all_plan_perms,
        )

    def query(self, query):
        with self.permissions():
            result = graphene.Schema(query=Query).execute(
                query,
                context_value=self.request(),
            )
        self.assertIsNone(result.errors, result.errors)
        return result.data

    def graphql_mutation(self, field, input_type, data):
        request = RequestFactory().post("/graphql")
        request.user = self.reader
        with self.permissions():
            result = graphene.Schema(
                query=Query,
                mutation=ContributionPlanMutation,
            ).execute(
                f'mutation($input:{input_type}!) {{ '
                f'{field}(input:$input) {{ internalId }} }}',
                context_value=request,
                variable_values={"input": data},
            )
        self.assertIsNone(result.errors, result.errors)
        return MutationLog.objects.get(pk=result.data[field]["internalId"])

    def add_plan_b_access(self):
        UserBenefitPlanAccess(
            user=self.reader,
            benefit_plan=self.plan_b,
        ).save(username=self.admin.username)

    def test_model_graphql_history_and_direct_nodes_are_scoped(self):
        visible = PaymentPlan.get_queryset(
            PaymentPlan.objects.all(), self.reader,
        )
        self.assertCountEqual(
            visible.values_list("code", flat=True),
            ["T06-PP-A", "T06-PP-LEG"],
        )

        data = self.query("""{
            paymentPlan { edges { node { code } } }
            paymentPlanHistory: paymentPlan(showHistory: true) {
                edges { node { code } }
            }
        }""")
        for field in ("paymentPlan", "paymentPlanHistory"):
            self.assertEqual(
                {edge["node"]["code"] for edge in data[field]["edges"]},
                {"T06-PP-A", "T06-PP-LEG"},
            )

        visible_node = to_global_id("PaymentPlanGQLType", self.payment_plan_a.pk)
        hidden_node = to_global_id("PaymentPlanGQLType", self.payment_plan_b.pk)
        data = self.query(
            f'{{ visible: node(id:"{visible_node}") {{ id }} '
            f'hidden: node(id:"{hidden_node}") {{ id }} }}'
        )
        self.assertIsNotNone(data["visible"])
        self.assertIsNone(data["hidden"])

    def test_feature_disabled_all_plan_and_superuser_compatibility(self):
        with patch.object(type(self.reader), "has_perms", return_value=False):
            with override_settings(ROW_SECURITY=False):
                SocialProtectionConfig.enable_benefit_plan_access_control = False
                self.assertEqual(
                    PaymentPlan.get_queryset(None, self.reader).count(),
                    PaymentPlan.objects.filter(is_deleted=False).count(),
                )

        SocialProtectionConfig.enable_benefit_plan_access_control = True
        with patch.object(type(self.reader), "has_perms", return_value=True):
            self.assertTrue(
                PaymentPlan.get_queryset(None, self.reader).filter(
                    pk=self.payment_plan_b.pk,
                ).exists()
            )
        self.assertTrue(
            PaymentPlan.get_queryset(None, self.admin).filter(
                pk=self.payment_plan_b.pk,
            ).exists()
        )

    def test_feature_disabled_preserves_null_target_update_and_replacement(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        null_target = PaymentPlan(
            code="T06-NULL-TARGET",
            name="Null target",
            calculation=uuid4(),
            benefit_plan_type=None,
            benefit_plan_id=None,
            periodicity=1,
        )
        null_target.save(username=self.admin.username)
        service = PaymentPlanService(self.reader)

        updated = service.update({
            "id": null_target.pk,
            "name": "Null target updated",
            "benefit_plan_type": None,
            "benefit_plan_id": None,
        })
        self.assertTrue(updated["success"], updated)
        null_target.refresh_from_db()
        self.assertIsNone(null_target.benefit_plan_type)
        self.assertIsNone(null_target.benefit_plan_id)

        replaced = service.replace({
            "uuid": null_target.pk,
            "name": "Null target replacement",
            "benefit_plan_type": None,
            "benefit_plan_id": None,
        })
        self.assertTrue(replaced["success"], replaced)
        replacement = PaymentPlan.objects.get(pk=replaced["uuid_new_object"])
        self.assertIsNone(replacement.benefit_plan_type)
        self.assertIsNone(replacement.benefit_plan_id)

    def test_feature_disabled_preserves_null_target_on_create(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False

        payment_plan = CreatePaymentPlanMutation.create_object(
            self.reader,
            {
                "code": "T06-NULL-CREATE",
                "name": "Null target create",
                "calculation": uuid4(),
                "periodicity": 1,
            },
        )

        self.assertIsNone(payment_plan.benefit_plan_type)
        self.assertIsNone(payment_plan.benefit_plan_id)

    def test_feature_disabled_missing_legacy_target_raises_validation_error(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        missing_product_id = self.product.pk + 1000000

        with self.assertRaisesRegex(
            ValidationError,
            "Referenced Payment Plan target does not exist",
        ):
            resolve_payment_plan_reference(
                self.reader,
                {
                    "benefit_plan_type__model": "product",
                    "benefit_plan_id": missing_product_id,
                },
            )

    def test_payment_plan_related_resolvers_keep_contribution_plan_permissions_and_access(self):
        info = SimpleNamespace(context=SimpleNamespace(user=self.reader))

        with patch.object(
            type(self.reader),
            "has_perms",
            side_effect=lambda perms: (
                perms == ContributionPlanConfig.gql_query_contributionplan_perms
            ),
        ):
            self.assertEqual(
                PaymentPlanGQLType.resolve_benefit_plan_type_name(
                    self.payment_plan_a,
                    info,
                ),
                self.benefit_plan_type.name,
            )
            self.assertIsNotNone(
                PaymentPlanGQLType.resolve_benefit_plan(
                    self.payment_plan_a,
                    info,
                )
            )
            with self.assertRaises(PermissionDenied):
                PaymentPlanGQLType.resolve_benefit_plan_type_name(
                    self.payment_plan_b,
                    info,
                )
            with self.assertRaises(PermissionDenied):
                PaymentPlanGQLType.resolve_benefit_plan(
                    self.payment_plan_b,
                    info,
                )

        with patch.object(type(self.reader), "has_perms", return_value=False):
            with self.assertRaises(PermissionDenied):
                PaymentPlanGQLType.resolve_benefit_plan_type_name(
                    self.payment_plan_a,
                    info,
                )
            with self.assertRaises(PermissionDenied):
                PaymentPlanGQLType.resolve_benefit_plan(
                    self.payment_plan_a,
                    info,
                )

    def test_feature_off_preserves_legacy_product_payment_plan(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        queryset = PaymentPlan.objects.filter(is_deleted=False)
        self.assertIs(
            filter_payment_plans_by_benefit_plan(queryset, self.reader),
            queryset,
        )
        content_type, object_id = resolve_payment_plan_reference(
            self.reader,
            {
                "benefit_plan_type": self.product_type,
                "benefit_plan_id": self.product.pk,
            },
        )
        require_payment_plan_access(self.reader, self.legacy_payment_plan)

        self.assertEqual(content_type, self.product_type)
        self.assertEqual(object_id, self.product.pk)

    def test_absent_social_protection_preserves_legacy_product_behavior(self):
        queryset = PaymentPlan.objects.filter(is_deleted=False)
        with patch(
            "contribution_plan.mixins.apps.is_installed",
            return_value=False,
        ), patch.dict(
            sys.modules,
            {"social_protection.benefit_plan_access": None},
        ):
            self.assertIs(
                filter_payment_plans_by_benefit_plan(queryset, self.reader),
                queryset,
            )
            require_payment_plan_access(self.reader, self.legacy_payment_plan)
            content_type, object_id = resolve_payment_plan_reference(
                self.reader,
                {
                    "benefit_plan_type": self.product_type,
                    "benefit_plan_id": self.product.pk,
                },
            )

        self.assertEqual(content_type, self.product_type)
        self.assertEqual(object_id, self.product.pk)

    def test_enabled_mode_fails_closed_without_shared_authorization(self):
        with patch.dict(
            sys.modules,
            {"social_protection.benefit_plan_access": None},
        ):
            with self.assertRaises(ImproperlyConfigured):
                filter_payment_plans_by_benefit_plan(
                    PaymentPlan.objects.all(), self.reader,
                )

    def test_enabled_mode_propagates_shared_authorization_denials(self):
        with patch(
            "social_protection.benefit_plan_access.get_accessible_benefit_plan_ids",
            side_effect=PermissionDenied("denied"),
        ):
            with self.assertRaises(PermissionDenied):
                filter_payment_plans_by_benefit_plan(
                    PaymentPlan.objects.all(), self.reader,
                )
        with patch(
            "social_protection.benefit_plan_access.require_benefit_plan_access",
            side_effect=PermissionDenied("denied"),
        ):
            with self.assertRaises(PermissionDenied):
                require_payment_plan_access(self.reader, self.payment_plan_a)

    def test_service_reads_and_mutations_reject_unauthorized_original_plan(self):
        service = PaymentPlanService(self.reader)
        response = service.get_by_id(SimpleNamespace(id=self.payment_plan_b.pk))
        self.assertFalse(response["success"])
        self.assertIn("matching query", response["detail"].lower())

        with self.permissions():
            with self.assertRaises(PermissionDenied):
                UpdatePaymentPlanMutation._validate_mutation(
                    self.reader,
                    id=self.payment_plan_b.pk,
                    benefit_plan_id=str(self.plan_b.pk),
                )
            with self.assertRaises(PermissionDenied):
                DeletePaymentPlanMutation._validate_mutation(
                    self.reader,
                    uuids=[self.payment_plan_b.pk],
                )
            with self.assertRaises(PermissionDenied):
                ReplacePaymentPlanMutation._validate_mutation(
                    self.reader,
                    uuid=self.payment_plan_b.pk,
                    benefit_plan_id=str(self.plan_b.pk),
                )

    def test_create_and_update_require_destination_plan_access(self):
        base_data = {
            "name": "Track 06 create",
            "calculation": uuid4(),
            "benefit_plan_type__model": "benefitplan",
            "benefit_plan_id": str(self.plan_b.pk),
            "periodicity": 1,
        }
        with self.permissions():
            with self.assertRaises(PermissionDenied):
                CreatePaymentPlanMutation.create_object(self.reader, base_data)
            with self.assertRaises(PermissionDenied):
                UpdatePaymentPlanMutation._validate_mutation(
                    self.reader,
                    id=self.payment_plan_a.pk,
                    benefit_plan_type__model="benefitplan",
                    benefit_plan_id=str(self.plan_b.pk),
                )

        self.add_plan_b_access()
        with self.permissions():
            UpdatePaymentPlanMutation._validate_mutation(
                self.reader,
                id=self.payment_plan_a.pk,
                benefit_plan_type__model="benefitplan",
                benefit_plan_id=str(self.plan_b.pk),
            )

    def test_graphql_mutations_reject_hidden_destination_and_atomic_batch_delete(self):
        update_log = self.graphql_mutation(
            "updatePaymentPlan",
            "UpdatePaymentPlanMutationInput",
            {
                "id": str(self.payment_plan_a.pk),
                "benefitPlanType_Model": "benefitplan",
                "benefitPlanId": str(self.plan_b.pk),
            },
        )
        self.assertEqual(update_log.status, MutationLog.ERROR)
        self.assertIn("Benefit Plan access", update_log.error)

        delete_log = self.graphql_mutation(
            "deletePaymentPlan",
            "DeletePaymentPlanMutationInput",
            {
                "uuids": [
                    str(self.payment_plan_a.pk),
                    str(self.payment_plan_b.pk),
                ],
            },
        )
        self.assertEqual(delete_log.status, MutationLog.ERROR)
        self.payment_plan_a.refresh_from_db()
        self.payment_plan_b.refresh_from_db()
        self.assertFalse(self.payment_plan_a.is_deleted)
        self.assertFalse(self.payment_plan_b.is_deleted)

    def test_graphql_create_allows_assigned_social_and_legacy_product_plans(self):
        social_log = self.graphql_mutation(
            "createPaymentPlan",
            "CreatePaymentPlanMutationInput",
            {
                "code": "T06-GQL-SP",
                "name": "Track 06 GraphQL social",
                "calculation": str(uuid4()),
                "benefitPlanType_Model": "benefitplan",
                "benefitPlanId": str(self.plan_a.pk),
                "periodicity": 1,
            },
        )
        legacy_log = self.graphql_mutation(
            "createPaymentPlan",
            "CreatePaymentPlanMutationInput",
            {
                "code": "T06-GQL-LEG",
                "name": "Track 06 GraphQL legacy",
                "calculation": str(uuid4()),
                "benefitPlanId": str(self.product.pk),
                "periodicity": 1,
            },
        )

        self.assertEqual(social_log.status, MutationLog.SUCCESS)
        self.assertEqual(legacy_log.status, MutationLog.SUCCESS)
        social = PaymentPlan.objects.get(code="T06-GQL-SP")
        legacy = PaymentPlan.objects.get(code="T06-GQL-LEG")
        self.assertEqual(social.benefit_plan_type, self.benefit_plan_type)
        self.assertEqual(social.benefit_plan_id, str(self.plan_a.pk))
        self.assertEqual(legacy.benefit_plan_type, self.product_type)
        self.assertEqual(legacy.benefit_plan_id, str(self.product.pk))

    def test_service_crud_enforces_access_and_preserves_legacy_products(self):
        service = PaymentPlanService(self.reader)
        unauthorized = service.create({
            "name": "Unauthorized Track 06",
            "calculation": uuid4(),
            "benefit_plan_type": self.benefit_plan_type,
            "benefit_plan_id": self.plan_b.pk,
            "periodicity": 1,
        })
        self.assertFalse(unauthorized["success"])
        self.assertIn("Benefit Plan access", unauthorized["detail"])

        legacy = service.create({
            "name": "Legacy Track 06",
            "calculation": uuid4(),
            "benefit_plan_type": self.product_type,
            "benefit_plan_id": self.product.pk,
            "periodicity": 1,
        })
        self.assertTrue(legacy["success"], legacy)

        hidden_update = service.update({
            "id": self.payment_plan_b.pk,
            "name": "Must not change",
        })
        hidden_delete = service.delete({"id": self.payment_plan_b.pk})
        hidden_replace = service.replace({"uuid": self.payment_plan_b.pk})
        self.assertFalse(hidden_update["success"])
        self.assertFalse(hidden_delete["success"])
        self.assertFalse(hidden_replace["success"])
        self.payment_plan_b.refresh_from_db()
        self.assertEqual(self.payment_plan_b.name, "T06-PP-B")
        self.assertFalse(self.payment_plan_b.is_deleted)

        blocked_transfer = service.update({
            "id": self.payment_plan_a.pk,
            "benefit_plan_id": self.plan_b.pk,
        })
        self.assertFalse(blocked_transfer["success"])
        self.assertIn("Benefit Plan access", blocked_transfer["detail"])

        self.add_plan_b_access()
        allowed_transfer = service.update({
            "id": self.payment_plan_a.pk,
            "benefit_plan_id": self.plan_b.pk,
        })
        self.assertTrue(allowed_transfer["success"], allowed_transfer)
        self.payment_plan_a.refresh_from_db()
        self.assertEqual(self.payment_plan_a.benefit_plan_id, str(self.plan_b.pk))

    def test_uuid_normalization_and_content_type_validation(self):
        content_type, normalized_id = resolve_payment_plan_reference(
            self.reader,
            {
                "benefit_plan_type": self.benefit_plan_type,
                "benefit_plan_id": self.plan_a.pk.hex.upper(),
            },
        )
        self.assertEqual(content_type, self.benefit_plan_type)
        self.assertEqual(normalized_id, str(self.plan_a.pk))

        with self.assertRaisesRegex(ValidationError, "valid UUID"):
            resolve_payment_plan_reference(
                self.reader,
                {
                    "benefit_plan_type": self.benefit_plan_type,
                    "benefit_plan_id": "not-a-uuid",
                },
            )
        with self.assertRaisesRegex(ValidationError, "only Product"):
            resolve_payment_plan_reference(
                self.reader,
                {
                    "benefit_plan_type": ContentType.objects.get_for_model(type(self.reader)),
                    "benefit_plan_id": self.reader.pk,
                },
            )

    def test_code_validation_is_global_across_hidden_payment_plans(self):
        with self.permissions():
            result = PaymentPlanService.check_unique_code(
                self.payment_plan_b.code,
                user=self.reader,
            )
        self.assertTrue(result)

        with self.permissions(), self.assertRaisesRegex(
            ValidationError, "payment_plan_code_duplicated",
        ):
            CreatePaymentPlanMutation._validate_mutation(
                self.reader,
                code=self.payment_plan_b.code,
            )
