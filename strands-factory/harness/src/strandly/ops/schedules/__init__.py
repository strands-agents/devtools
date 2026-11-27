"""Scheduled jobs: the DynamoDB store, EventBridge Scheduler sync, and the service over both.

stdlib + boto3 only (ships in the Lambda bundle). Entry point: ``service.ScheduleService``
(every read and write).
"""
