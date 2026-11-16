from django.urls import path

from jobengine.web import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("results/", views.results, name="results"),
    path("start/", views.start, name="start"),
    path("demo/", views.demo_start, name="demo_start"),
    path("demo/profile/", views.demo_profile, name="demo_profile"),
    path("demo/plan/", views.demo_plan, name="demo_plan"),
    path("demo/interview/", views.demo_interview, name="demo_interview"),
    path("demo/results/", views.demo_results, name="demo_results"),
    path("conversation/", views.interview, name="interview"),
    path("profile/", views.profile, name="profile"),
    path("preferences/", views.preferences, name="preferences"),
    path("plan/", views.plan, name="plan"),
    path("opportunities/", views.opportunities, name="opportunities"),
    path("saved/", views.saved, name="saved"),
    path("applied/", views.applied, name="applied"),
    path("runs/<str:identifier>/jobs/<int:position>/", views.job_detail, name="job_detail"),
    path("runs/<str:identifier>/jobs/<int:position>/save/", views.bookmark, name="bookmark"),
    path(
        "runs/<str:identifier>/jobs/<int:position>/application/",
        views.application,
        name="application",
    ),
    path("activity/<str:identifier>/", views.activity, name="activity"),
    path("activity/<str:identifier>/retry/", views.retry, name="retry"),
    path("library/", views.library, name="library"),
    path("restore/", views.restore, name="restore"),
    path("new/", views.new_profile, name="new_profile"),
    path("settings/", views.configuration, name="settings"),
    path("export/<str:kind>/<str:identifier>/", views.export, name="export"),
]
