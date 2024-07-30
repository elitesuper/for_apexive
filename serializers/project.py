from core.models import Project, ProjectMembership
from rest_framework import serializers

from .project_membership import ProjectMembershipSerializer


class ProjectSerializer(serializers.ModelSerializer):

    teams = ProjectMembershipSerializer(
        many=True, required=False, source="project_membership"
    )

    class Meta:
        model = Project
        fields = ("id", "name", "has_public_co2_reporting", "gardener_enabled", "teams")

    def create(self, validated_data):
        teams_data = validated_data.pop("project_membership", [])
        project = Project.objects.create(**validated_data)
        for team_data in teams_data:
            ProjectMembership.objects.create(project=project, **team_data)
        return project

    def update(self, instance, validated_data):
        teams_data = validated_data.pop("project_membership", [])
        instance = super().update(instance, validated_data)

        ProjectMembership.objects.exclude(
            project=instance, team__in=[team_data["team"] for team_data in teams_data]
        ).delete()

        for team_data in teams_data:
            ProjectMembership.objects.update_or_create(
                project=instance, team=team_data.pop("team"), defaults=team_data
            )

        return instance
