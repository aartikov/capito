"""The base of command sets: a project subclasses Commands once per set and lists the classes in Client.commands."""


class Commands:
    """A command set bound to a client: methods marked @command, available as client.<name>.

    A project subclasses it once per set (EventsCommands, ...) and lists the classes in Client.commands.
    Commands send requests with self.client.call(...).
    """
    def __init__(self, client):
        self.client = client
