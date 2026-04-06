from django import template

register = template.Library()


@register.filter
def split_choices(value):
    """
    Convert a pipe+comma string into (value, label) pairs for sort links.
    Usage: "featured:Best match,rating:Highest rated"|split_choices
    """
    pairs = []
    for item in value.split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            pairs.append((k.strip(), v.strip()))
    return pairs
