"""
Cat Monitor Tool for OpenWebUI.

Upload via: OpenWebUI → Settings → Tools → Add Tool (paste this file).
Enable in chat by clicking the tools icon before sending a message.
"""

import json
from datetime import date

import requests

CAT_OBSERVER_URL = "http://host.docker.internal:8088"


class Tools:
    def __init__(self):
        self.citation = True

    def get_daily_cat_summary(self, date_str: str = "", camera: str = "") -> str:
        """
        Get a summary of cat activity for a specific date, including total observations,
        breakdown by activity (sleeping, playing, eating, etc.), and a full timeline.
        Use this when the user asks what their cats did on a given day, how active
        the cats were, or wants a daily overview.

        :param date_str: Date in YYYY-MM-DD format. Leave empty for today.
        :param camera: Filter by camera ('living_room' or 'bedroom'). Leave empty for all.
        :return: Summary with cat observation counts, activities, and timeline.
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

            lines = [
                f"Date: {data.get('date', date_str or str(date.today()))}",
                f"Total observations: {data.get('total_observations', 0)}",
                f"Observations with cats: {data.get('cat_observations', 0)}",
            ]
            if data.get("by_activity"):
                lines.append("Cat activity breakdown:")
                for a in data["by_activity"]:
                    lines.append(f"  - {a['activity_tag'] or 'unknown'}: {a['count']} times")
            if data.get("by_camera"):
                lines.append("By camera:")
                for c in data["by_camera"]:
                    lines.append(f"  - {c['camera_name']}: {c['count']} observations")
            if data.get("timeline"):
                cat_obs = [o for o in data["timeline"] if o.get("has_cat")]
                if cat_obs:
                    lines.append(f"\nFirst cat seen: {cat_obs[0]['timestamp']}")
                    lines.append(f"Last cat seen:  {cat_obs[-1]['timestamp']}")

            lines.append("\nRaw data:\n" + json.dumps(data, default=str, indent=2))
            return "\n".join(lines)

        except requests.RequestException as e:
            return f"Error contacting cat-observer: {e}"

    def get_recent_cat_observations(
        self,
        limit: int = 10,
        camera: str = "",
        activity: str = "",
        cats_only: bool = True,
    ) -> str:
        """
        Retrieve the most recent observations with VILA descriptions of what the cameras saw.
        Use this when the user wants to know the latest activity, asks what their cats are
        doing right now, or wants specific observation details with descriptions.

        :param limit: Number of recent observations to return (1–50).
        :param camera: Filter by camera ('living_room' or 'bedroom'). Leave empty for all.
        :param activity: Filter by activity (e.g. 'sleeping', 'playing', 'eating').
        :param cats_only: If True, only return observations where cats were visible.
        :return: List of recent observations with VILA descriptions.
        """
        params: dict = {"limit": min(max(limit, 1), 50)}
        if camera:
            params["camera"] = camera
        if activity:
            params["activity"] = activity
        if cats_only:
            params["has_cat"] = "true"

        try:
            resp = requests.get(f"{CAT_OBSERVER_URL}/observations", params=params, timeout=10)
            resp.raise_for_status()
            observations = resp.json()

            if not observations:
                return "No observations found matching the criteria."

            lines = [f"Found {len(observations)} observation(s):\n"]
            for obs in observations:
                cat_info = f"{obs.get('cat_count', '?')} cat(s)" if obs.get("has_cat") else "no cats"
                lines.append(
                    f"[{obs.get('timestamp', '')}] {obs.get('camera_name', '?')} — "
                    f"{cat_info}, {obs.get('activity_tag', '?')} at {obs.get('location_tag', '?')}"
                )
                if obs.get("description"):
                    lines.append(f"  VILA: {obs['description']}")

            lines.append("\nRaw data:\n" + json.dumps(observations, default=str, indent=2))
            return "\n".join(lines)

        except requests.RequestException as e:
            return f"Error contacting cat-observer: {e}"

    def check_cat_system_health(self) -> str:
        """
        Check whether the cat monitoring system is running correctly.
        Use this when the user asks if the cameras are online, whether the system
        is working, or if there are any problems with monitoring.

        :return: System health including database status, cameras, and nano-llm URL.
        """
        try:
            resp = requests.get(f"{CAT_OBSERVER_URL}/health", timeout=5)
            resp.raise_for_status()
            data = resp.json()

            lines = [
                f"System status: {data.get('status', 'unknown')}",
                f"Database: {data.get('db', 'unknown')}",
                f"Cameras monitored: {', '.join(data.get('cameras', []))}",
                f"Poll interval: {data.get('poll_interval_seconds', '?')}s",
                f"nano-llm: {data.get('nano_llm_url', '?')}",
            ]
            return "\n".join(lines)

        except requests.RequestException as e:
            return f"cat-observer is unreachable: {e}"
