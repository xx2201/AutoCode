# The exact line `return "placeholder"` appears twice below.
# A minimal edit on that shared line is intentionally ambiguous.

def build_message():
    return "placeholder"


def build_status():
    return "placeholder"


if __name__ == "__main__":
    print(build_message())
    print(build_status())
