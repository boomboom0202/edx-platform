"""Packaging for the EKTU Halyk ePay integration."""
from setuptools import find_packages, setup

setup(
    name="ektu-halyk-payments",
    version="0.1.0",
    description="Halyk ePay payments for the EKTU Open edX platform",
    author="EKTU Center for Educational Technologies",
    packages=find_packages(exclude=["tests", "tests.*"]),
    include_package_data=True,
    package_data={"halyk_payments": ["templates/halyk_payments/*.html"]},
    # The LMS half of this package runs on the container's Python, but the Tutor
    # half runs on the host's, which is commonly older -- so the floor is what
    # the code actually needs, not what Open edX happens to ship.
    python_requires=">=3.8",
    install_requires=["requests"],
    entry_points={
        # Discovered by the LMS: registers the URLs and settings declared in
        # halyk_payments/apps.py without patching edx-platform.
        "lms.djangoapp": [
            "halyk_payments = halyk_payments.apps:HalykPaymentsConfig",
        ],
        # Discovered by Tutor.
        "tutor.plugin.v1": [
            "ektu-halyk = tutor_ektu_halyk.plugin",
        ],
    },
    classifiers=["Private :: Do Not Upload"],
)
