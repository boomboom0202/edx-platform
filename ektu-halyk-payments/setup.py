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
    python_requires=">=3.11",
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
