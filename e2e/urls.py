from django.urls import include, path

urlpatterns = [
    path("auth/api/", include("stapel_auth.urls")),
    path("workspaces/api/", include("stapel_workspaces.urls")),
    path("docs/", include("stapel_docs.urls")),
]
