from django.urls import path

from . import staff_sign_in

urlpatterns = [
    path('callback/', staff_sign_in.callback, name='staff-sign-in-callback'),
    path('invitation/<str:token>/', staff_sign_in.accept_invitation, name='staff-sign-in-invitation'),
    path('<slug:key>/', staff_sign_in.start, name='staff-sign-in'),
]
