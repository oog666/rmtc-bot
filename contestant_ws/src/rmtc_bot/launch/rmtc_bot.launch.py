"""一键启动：ros2 launch rmtc_bot rmtc_bot.launch.py team:=A team_name:=图灵队"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("team", default_value="A"),
        DeclareLaunchArgument("team_name", default_value="图灵队"),
        DeclareLaunchArgument("ai_gateway", default_value="http://127.0.0.1:8080/v1/chat/completions"),
        DeclareLaunchArgument("ai_model", default_value=""),
        DeclareLaunchArgument("decision_config", default_value="decision_config.json"),
        Node(
            package="rmtc_bot",
            executable="rmtc_bot",
            name="rmtc_bot",
            output="screen",
            parameters=[{
                "team": LaunchConfiguration("team"),
                "team_name": LaunchConfiguration("team_name"),
                "ai_gateway": LaunchConfiguration("ai_gateway"),
                "ai_model": LaunchConfiguration("ai_model"),
                "decision_config": LaunchConfiguration("decision_config"),
            }],
        ),
    ])
