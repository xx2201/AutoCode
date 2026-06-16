from app.api import build_report, close_issue


def main():
    print(build_report(" open "))
    close_issue("I-100")
    print(build_report("closed"))


if __name__ == "__main__":
    main()
