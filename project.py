import logging
from datetime import datetime

from billing.services import CloudKitty
from core.services.gardener_service import GardenerService
from core.services.openstack import Openstack
from core.tasks import authorize_openstack_users as authorize_openstack_users_task
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


class ProjectQuerySet(models.QuerySet):
    """Queryset for Project"""

    def for_user(self, user):
        query_set = self.filter(organization__in=user.organizations.all())

        if user.has_perm("core.can_manage_all_organization_projects"):
            return query_set.distinct()

        return query_set.filter(teams__in=user.teams.all()).distinct()

    def for_user_is_owner_of_organization(self, user):
        return self.filter(organization__organization_memberships__user=user).distinct()


class Project(models.Model):

    objects = ProjectQuerySet.as_manager()
    organization = models.ForeignKey(
        "Organization", null=True, related_name="projects", on_delete=models.CASCADE
    )
    name = models.CharField(
        max_length=71,
        unique=True,
        blank=False,
        help_text="Natural Key, should not be changed",
    )
    description = models.CharField(max_length=128, unique=False, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    modified_at = models.DateTimeField(auto_now=True, editable=False)
    deleted_at = models.DateTimeField(default=None, null=True, editable=False)
    openstack_id = models.CharField(max_length=40, blank=True)
    billing_contact = models.ForeignKey(
        "billing.Contact",
        blank=True,
        null=True,
        related_name="projects",
        on_delete=models.SET_NULL,
    )
    enabled = models.BooleanField(default=False)
    has_public_co2_reporting = models.BooleanField(default=False)
    keycloak_id = models.CharField(default=None, max_length=50, null=True, blank=True)
    gardener_id = models.CharField(default=None, max_length=10, null=True, blank=True)
    gardener_enabled = models.BooleanField(default=False)

    mails = models.ManyToManyField(
        "Mail", through="NotificationSentToProject", related_name="projects"
    )

    class Meta:
        verbose_name = _("Project")
        verbose_name_plural = _("Projects")
        permissions = [
            (
                "can_manage_all_organization_projects",
                _("Can manage all organization projects"),
            ),
        ]

    def save(self, *args, **kwargs):
        save_to_gardener = kwargs.pop("save_to_gardener", True)
        super(Project, self).save(*args, **kwargs)
        if save_to_gardener:
            GardenerService.save_project(self)

        return self

    def __str__(self):
        return self.name

    def get_rate(self, start=None, end=None):
        """
        This function gets the usage from openstack cloudkitty,
        for a specified time range.
        """
        if not self.openstack_id:
            return None
        return CloudKitty().get_report(self.openstack_id, start=start, end=end)

    def get_compute_usage(self, start=None, end=None):
        # TODO: perhaps better to make new time object,
        #       and is since 1st of this month most logical?
        if not start:
            start = datetime.now().replace(day=1, hour=0, minute=0, second=0)
        if not end:
            end = datetime.now().replace(hour=0, minute=0, second=0)

        cache_key = "compute_usage.{}.{}-{}".format(
            self.openstack_id,
            start.strftime("%Y-%b-%d"),
            end.strftime("%Y-%b-%d"),
        )

        if not self.openstack_id:
            return None
        result = cache.get(cache_key)
        if result is not None:
            return result

        result = Openstack().get_compute_usage_for_project(
            self.openstack_id, start=start, end=end
        )
        cache.set(cache_key, result, settings.REPORTING_CACHE_TIMEOUT)
        return result

    def usage_summary(self, start=None, end=None):
        server_usages = self.get_compute_usage(start, end)

        if not server_usages:
            return None

        def filter_active(item):
            if item["state"] == "active":
                return item

        server_usages = list(map(filter_active, server_usages))

        if server_usages:
            return {
                "total": len(server_usages),
                "ram": sum(s["memory_mb"] for s in server_usages if s),
                "vcpus": sum(s["vcpus"] for s in server_usages if s),
            }
        else:
            return None

    @property
    def usage_total(self):
        server_usages = self.get_compute_usage()

        if not server_usages:
            return None

        def filter_active(item):
            if item["state"] == "active":
                return item

                # ADD vcpu total

        results = list(map(filter_active, server_usages["server_usages"]))
        if results:
            return len(results)
        else:
            return None

    def get_volume_list(self):
        volume_list = Openstack().list_volumes(self.openstack_id)
        return volume_list

    def get_server_list(self):
        return Openstack().list_servers(self.openstack_id)

    def create_openstack_project(self):
        if self.openstack_id:
            logger.warning(
                f"Project '{self.name}' already has an "
                f"OpenStack ID: {self.openstack_id}. No new project will be created."
            )
            return

        os_project = Openstack().create_project(self.name)
        self.openstack_id = os_project.id
        self.save()
        return os_project

    def authorize_openstack_users(self):
        authorize_openstack_users_task.delay(self.id)

    def grant_openstack_rights_to_user(self, os_user):
        if not os_user.profile.openstack_id or not self.openstack_id:
            return
        openstack_id = self.openstack_id
        os_client = Openstack()
        os_client.grant_tenant_roles(os_user.profile.openstack_id, openstack_id)

    def set_gpu_quota(self):
        os_client = Openstack()
        os_client.set_gpu_quota(self.openstack_id)

    def get_members(self):
        User = apps.get_model(app_label="core", model_name="User")
        members = User.objects.filter(teams__projects=self)
        return members

    def get_owners(self):
        User = apps.get_model(app_label="core", model_name="User")
        owners = User.objects.filter(teams__projects=self).filter(
            teammembership__role="owner"
        )
        return owners

    def is_active(self):
        """
        check if one of its plans is active
        """
        has_active = False

        for plan in self.plans.all().filter(
            active=True, valid_from__lte=timezone.now()
        ):
            if not plan.expiration_time:
                has_active = True
            elif plan.expiration_time > timezone.now():
                has_active = True
        return has_active

    is_active.boolean = True

    @property
    def enabled_on_openstack(self):
        try:
            return Openstack().get_project(self.openstack_id).enabled
        except Exception:
            return False


@receiver(post_delete, sender=Project)
def delete_project(sender, instance: Project, **kwargs):
    GardenerService.delete_project(instance)


class ProjectMembership(models.Model):
    """
    Relationship between projects and teams.
    Team.objects.Project.all()
    and
    Project.objects.Team_set.all()
    """

    ADMIN = "admin"
    WRITE = "write"
    READ = "read"
    ROLE_CHOICES = (
        (ADMIN, "Admin"),
        (WRITE, "Write"),
        (READ, "Read"),
    )

    verbose_name = "YY team membership"
    verbose_name_plural = "YY team memberships"

    project = models.ForeignKey(
        Project, related_name="project_membership", on_delete=models.CASCADE
    )
    team = models.ForeignKey(
        "Team", related_name="project_membership", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=8, choices=ROLE_CHOICES, default=ADMIN)

    def save(self, *args, **kwargs):
        super(ProjectMembership, self).save(*args, **kwargs)

        GardenerService.save_project(self.project)
        return self


@receiver(post_delete, sender=ProjectMembership)
def delete_project_membership(sender, instance: ProjectMembership, **kwargs):
    GardenerService.delete_project(instance.project)
