from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.contrib.auth.models import User
from src.users.serializers import UserDetailSerializer


@api_view(["GET"])
def user_detail(request, pk):
    try:
        user = User.objects.get(pk=pk)
        serializer = UserDetailSerializer(user)
        return Response(serializer.data)
    except User.DoesNotExist:
        return Response({"error": "not found"}, status=404)
