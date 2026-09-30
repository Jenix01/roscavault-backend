import uuid
from django.db import models

class AbstractBaseModel(models.Model):
    """
    An abstract base class that provides a UUID primary key 
    and timestamps for all models in the project.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True