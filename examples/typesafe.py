from __future__ import annotations

from pathlib import Path

from typesafe_sdk import Choice, Noul, SystemOneResponse, TypeSafeClient

from aifn import AiFunction, Flow, Link, Mailbox, Model, Policy, Registry, Stage, Worker
from aifn.engines import TypeSafeEngine, TypeSafeRequest

judge = AiFunction(
    name="judge", request=TypeSafeRequest, returns=SystemOneResponse,
    model=Model(name="jev-1.13.0", provider="typesafe"),
    timeout_s=30,
)


def follow_up(answer: SystemOneResponse) -> list[TypeSafeRequest]:
    return [TypeSafeRequest(
        state={"selected_category": answer.choices["category"].choice},
        questions={"billing_team": Noul(
            instructions="Is the selected category about a billing issue?",
        )},
    )]


def main() -> None:
    mailbox = Mailbox(root=Path("mailboxes/typesafe"))
    stage = Stage(function=judge, mailbox=mailbox)
    with TypeSafeClient() as client:
        worker = Worker(
            mailbox=mailbox, registry=Registry(functions={judge.name: judge}),
            engine=TypeSafeEngine(client=client), policy=Policy(), id="typesafe",
        )
        batch = stage.issue_all([TypeSafeRequest(
            state={"message": "I was charged twice. Please refund the duplicate."},
            questions={
                "category": Choice(instructions="What is the customer's issue?", criteria={
                    "billing": "Charges, invoices or refunds",
                    "technical": "Software or device malfunctions",
                    "other": "Anything else",
                }),
                "refund": Noul(instructions="Does the customer explicitly request a refund?"),
            },
        )])
        worker.work()
        for result in batch.settled():
            print(result.unwrap().model_dump_json(indent=2))
        flow = Flow(upstream=batch, downstream=stage, link=Link(adapt=follow_up))
        flow.advance()
        worker.work()
        for result in flow.issued().settled():
            print(result.unwrap().model_dump_json(indent=2))


if __name__ == "__main__":
    main()
