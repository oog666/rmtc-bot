from setuptools import find_packages, setup

package_name = "rmtc_bot"

setup(
    name=package_name,
    version="1.1.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", ["launch/rmtc_bot.launch.py"]),
        ("share/" + package_name + "/config", ["config/params.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="RMTC Team",
    maintainer_email="team@example.com",
    description="RMTC 图灵对决赛参赛程序",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "rmtc_bot = rmtc_bot.node:main",
        ],
    },
)
