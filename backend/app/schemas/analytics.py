from datetime import datetime

from pydantic import BaseModel


class AnalyticsOverview(BaseModel):
    total_calls: int
    total_minutes: float
    avg_duration_seconds: float
    total_cost_cents: int
    calls_by_status: dict[str, int]
    calls_by_direction: dict[str, int]
    calls_by_disposition: dict[str, int]


class AnalyticsTimeSeries(BaseModel):
    period: str
    data: list[dict]  # [{"date": "2024-01-01", "calls": 10, "minutes": 50.5}]


class AgentPerformance(BaseModel):
    agent_id: str
    agent_name: str
    total_calls: int
    avg_duration_seconds: float
    success_rate: float
    avg_sentiment: float | None


class CampaignAnalytics(BaseModel):
    campaign_id: str
    campaign_name: str
    total_contacts: int
    completed: int
    successful: int
    completion_rate: float
    success_rate: float
