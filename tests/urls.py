from django.urls import include, path

urlpatterns = [
    path("docs/", include("stapel_docs.urls")),
]
