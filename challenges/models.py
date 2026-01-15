from django.db import models

from social.models import (
    Challenge as SocialChallenge,
    Achievement as SocialAchievement,
    ChallengeParticipation as SocialChallengeParticipation,
    UserAchievement as SocialUserAchievement,
    Leaderboard as SocialLeaderboard,
    Notification as SocialNotification,
)


class ChallengeProxy(SocialChallenge):
    class Meta:
        proxy = True
        verbose_name = "Challenge"
        verbose_name_plural = "Challenges"


class AchievementProxy(SocialAchievement):
    class Meta:
        proxy = True
        verbose_name = "Achievement"
        verbose_name_plural = "Achievements"


class ChallengeParticipationProxy(SocialChallengeParticipation):
    class Meta:
        proxy = True
        verbose_name = "Challenge Participation"
        verbose_name_plural = "Challenge Participations"


class UserAchievementProxy(SocialUserAchievement):
    class Meta:
        proxy = True
        verbose_name = "User Achievement"
        verbose_name_plural = "User Achievements"


class LeaderboardProxy(SocialLeaderboard):
    class Meta:
        proxy = True
        verbose_name = "Leaderboard"
        verbose_name_plural = "Leaderboards"


class NotificationProxy(SocialNotification):
    class Meta:
        proxy = True
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
