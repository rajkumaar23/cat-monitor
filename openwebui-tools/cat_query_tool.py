"""
Cat Monitor Tool for OpenWebUI.

Upload via: OpenWebUI → Settings → Tools → Add Tool (paste this file).
Enable in chat by clicking the tools icon before sending a message.

The tool talks to cat-observer running at CAT_OBSERVER_URL (set below).
"""

import json
from datetime import date

import requests

CAT_OBSERVER_URL = "http://host.docker.internal:8088"


class Tools:
    def __init__(self):
        self.citation = True

    def get_daily_cat_summary(
        self,
        date_str: str = "",
        camera: str = "",
    ) -> str:
        """
        Get a summary of cat activity for a specific date, including total events,
        breakdown by activity (sleeping, playing, eating, etc.), and a timeline.
        Use this when the user asks what their cats did on a given day, how active
        the cats were, or for a daily overview.

        :param date_str: Date in YYYY-MM-DD format. Leave empty for today.
        :param camera: Filter to a specific camera (e.g. 'living_room', 'bedroom').
                       Leave empty for all cameras.
        :return: JSON string with total_events, by_activity, by_camera, and timeline.
        """
        params = {}
        if date_str:
            params["date"] = date_str
        if camera:
            params["camera"] = camera

        try:
            resp = requests.get(f"{CAT_OBSERVER_URL}/summary", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            # Build a human-readable summary to help the LLM narrate
            lines = [
                f"Date: {data.get('date', date_str or str(date.today()))}",
                f"Total events detected: {data.get('total_events', 0)}",
            ]
            if data.get("by_activity"):
                lines.append("Activity breakdown:")
                for a in data["by_activity"]:
                    lines.append(f"  - {a['activity_tag']}: {a['count']} times")
            if data.get("by_camera"):
                lines.append("By camera:")
                for c in data["by_camera"]:
                    lines.append(f"  - {c['camera_name']}: {c['count']} events")
            if data.get("timeline"):
                lines.append(f"\nFirst event: {data['timeline'][0]['timestamp']}")
                lines.append(f"Last event:  {data['timeline'][-1]['timestamp']}")

            lines.append("\nRaw data:\n" + json.dumps(data, default=str, indent=2))
            return "\n".join(lines)

        except requests.RequestException as e:
            return f"Error contacting cat-observer: {e}"

    def get_recent_cat_observations(
        self,
        limit: int = 10,
        camera: str = "",
        activity: str = "",
    ) -> str:
        """
        Retrieve the most recent cat observations with descriptions of what the cats
        were doing. Use this when the user wants to know the latest activity, asks
        'what are my cats doing right now', or wants specific observation details
        like descriptions or locations.

        :param limit: Number of recent observations to return (1–50).
        :param camera: Filter by camera name ('living_room' or 'bedroom').
                       Leave empty for all cameras.
        :param activity: Filter by activity type (e.g. 'sleeping', 'playing',
                         'eating', 'grooming'). Leave empty for all activities.
        :return: JSON string with a list of observations.
        """
        params: dict = {"limit": min(max(limit, 1), 50)}
        if camera:
            params["camera"] = camera
        if activity:
            params["activity"] = activity

        try:
            resp = requests.get(
                f"{CAT_OBSERVER_URL}/observations", params=params, timeout=10
            )
            resp.raise_for_status()
            observations = resp.json()

            if not observations:
                return "No observations found matching the criteria."

            lines = [f"Found {len(observations)} observation(s):\n"]
            for obs in observations:
                lines.append(
                    f"[{obs.get('timestamp', '')}] "
                    f"{obs.get('camera_name', '?')} — "
                    f"{obs.get('activity_tag', '?')} at {obs.get('location_tag', '?')} "
                    f"({obs.get('cat_count', '?')} cat(s))"
                )
                if obs.get("raw_description"):
                    lines.append(f"  Description: {obs['raw_description']}")

            lines.append("\nRaw data:\n" + json.dumps(observations, default=str, indent=2))
            return "\n".join(lines)

        except requests.RequestException as e:
            return f"Error contacting cat-observer: {e}"

    def check_cat_system_health(self) -> str:
        """
        Check whether the cat monitoring system is running correctly.
        Use this when the user asks if the cameras are online, whether the system
        is working, or if there are any problems with monitoring.

        :return: System health status including database, camera availability, and queue depth.
        """
        try:
            resp = requests.get(f"{CAT_OBSERVER_URL}/health", timeout=5)
            resp.raise_for_status()
            data = resp.json()

            cameras = data.get("cameras", {})
            cam_lines = [
                f"  - {cam}: {status}" for cam, status in cameras.items()
            ] or ["  (no camera status available yet)"]

            lines = [
                f"System status: {data.get('status', 'unknown')}",
                f"Database: {data.get('db', 'unknown')}",
                f"Event queue depth: {data.get('queue_depth', 0)}",
                "Cameras:",
            ] + cam_lines

            return "\n".join(lines)

        except requests.RequestException as e:
            return f"cat-observer is unreachable: {e}"
