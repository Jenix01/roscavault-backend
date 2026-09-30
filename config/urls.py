from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework_simplejwt.views import TokenRefreshView, TokenObtainPairView

urlpatterns = [

    path('admin/', admin.site.urls),

    # API Authentication Endpoints

    path('api/login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),

    path('api/login/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

   
    path('api/users/', include('users.urls')),

    path('api/contributions/', include('apps.contributions.urls')),

   
] 

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
