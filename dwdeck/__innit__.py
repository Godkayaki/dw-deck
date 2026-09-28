"""
Dw-Deck's application logic, split out of what used to be one large
app.py: decklist parsing, Moxfield import, Scryfall lookups, and
filename formatting each get their own module. app.py itself just wires
Flask up and registers the routes defined in dwdeck.routes.
"""