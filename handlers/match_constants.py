"""Match constants."""
import random

STADIUMS = [
    "Eden Gardens", "Wankhede Stadium", "Lord's Cricket Ground",
    "Melbourne Cricket Ground", "Sydney Cricket Ground", "Adelaide Oval",
    "The Oval", "M.A. Chidambaram Stadium", "Narendra Modi Stadium",
    "M.Chinnaswamy Stadium", "Newlands Cricket Ground", "Wanderers Stadium",
    "Galle International Stadium", "R. Premadasa Stadium",
    "Sharjah Cricket Stadium", "Sheikh Zayed Cricket Stadium",
    "Kensington Oval", "Sabina Park", "Rawalpindi Cricket Stadium",
    "National Stadium Karachi",
]

UMPIRES = [
    "Kumar Dharmasena", "Chris Gaffaney", "Adrian Holdstock",
    "Richard Illingworth", "Richard Kettleborough", "Nitin Menon",
    "Allahudien Paleker", "Ahsan Raza", "Paul Reiffel",
    "Sharfuddoula Ibne Shahid", "Rod Tucker", "Alex Wharf",
    "Roland Black", "Chris Brown", "Wayne Knights", "Donovan Koch",
    "Jayaraman Madanagopal", "Sam Nogajski", "Langton Rusere", "Asif Yaqoob",
]

PITCH_TYPES = ["Dry", "Dusty", "Hard", "Flat", "Green", "Bouncy"]
WEATHER = ["Mostly Sunny", "Sunny", "Cloudy", "Partly Cloudy", "Overcast"]
MATCH_EXPIRE = 30  # seconds


def random_match_settings():
    umps = random.sample(UMPIRES, 2)
    return {
        "stadium": random.choice(STADIUMS),
        "pitch_type": random.choice(PITCH_TYPES),
        "weather": random.choice(WEATHER),
        "temperature": random.randint(24, 39),
        "umpire1": umps[0],
        "umpire2": umps[1],
    }
