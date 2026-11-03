"""Tests for the Edufusion catalogue."""
from django.test import TestCase
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.student.tests.factories import UserFactory
from openedx.core.djangoapps.content.course_overviews.tests.factories import CourseOverviewFactory

URL = "/edufusion/"


def course(org, number, name, visibility="about"):
    key = CourseKey.from_string(f"course-v1:{org}+{number}+2026")
    return CourseOverviewFactory(
        id=key, org=org, display_number_with_default=number, display_name=name, catalog_visibility=visibility,
    )


class CatalogueTest(TestCase):

    def setUp(self):
        super().setUp()
        course("EDUFUSION", "EF02", "Second Edufusion")
        course("EDUFUSION", "EF01", "First Edufusion")
        course("EDUFUSION", "EF03", "Hidden Edufusion", visibility="none")
        course("ACD", "X1", "Ordinary course", visibility="both")
        course("EDUFUSION", "EF08", "Eighth Edufusion")
        key = CourseKey.from_string("course-v1:VKTU+PBLaMP+2026_C1")
        CourseOverviewFactory(
            id=key, org="VKTU", display_number_with_default="PBLaMP",
            display_name="Listed from another org", catalog_visibility="about",
        )

    def test_lists_only_edufusion_courses_in_order(self):
        content = self.client.get(URL).content.decode()
        assert content.index("First Edufusion") < content.index("Second Edufusion")
        assert "Hidden Edufusion" not in content
        assert "Ordinary course" not in content

    def test_lists_a_course_from_another_org_in_its_place(self):
        content = self.client.get(URL).content.decode()
        assert content.index("Second Edufusion") < content.index("Listed from another org")
        assert content.index("Listed from another org") < content.index("Eighth Edufusion")

    def test_shows_both_logos(self):
        content = self.client.get(URL).content.decode()
        assert content.index("<h1>") < content.index("edufusion-nsg-logo")
        assert content.index("edufusion-nsg-logo") < content.index("eu-co-funded")

    def test_main_catalog_leaves_them_out(self):
        content = self.client.get("/courses").content.decode()
        assert "Ordinary course" in content
        assert "First Edufusion" not in content

    def test_header_links_to_the_page(self):
        assert URL.encode() in self.client.get("/courses").content
        user = UserFactory()
        self.client.login(username=user.username, password="Password1234")
        assert URL.encode() in self.client.get("/help").content
