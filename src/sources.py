"""
World's Front Page — Master Source List
84 publications across 50+ countries.
Each source: (id, country, region, name, url, status, icij)
Status: "independent" | "state_organ" | "state_affiliated" | "state_adjacent" | "exile" | "pressured"
"""

SOURCES = [
    # ── AMERICAS ──────────────────────────────────────────────────────────────
    {"id": "nyt",         "country": "USA",          "region": "Americas",        "name": "The New York Times",           "url": "https://www.nytimes.com",                     "status": "independent",    "icij": True},
    {"id": "wsj",         "country": "USA",          "region": "Americas",        "name": "The Wall Street Journal",      "url": "https://www.wsj.com",                         "status": "independent",    "icij": False},
    {"id": "wapo",        "country": "USA",          "region": "Americas",        "name": "The Washington Post",          "url": "https://www.washingtonpost.com",               "status": "independent",    "icij": True},
    {"id": "globe_mail",  "country": "Canada",       "region": "Americas",        "name": "The Globe and Mail",           "url": "https://www.theglobeandmail.com",              "status": "independent",    "icij": True},
    {"id": "tor_star",    "country": "Canada",       "region": "Americas",        "name": "Toronto Star",                 "url": "https://www.thestar.com",                     "status": "independent",    "icij": False},
    {"id": "el_universal","country": "Mexico",       "region": "Americas",        "name": "El Universal",                 "url": "https://www.eluniversal.com.mx",              "status": "independent",    "icij": False},
    {"id": "reforma",     "country": "Mexico",       "region": "Americas",        "name": "Reforma",                      "url": "https://www.reforma.com",                     "status": "independent",    "icij": False},
    {"id": "folha",       "country": "Brazil",       "region": "Americas",        "name": "Folha de S.Paulo",             "url": "https://www.folha.uol.com.br",                "status": "independent",    "icij": True},
    {"id": "oglobo",      "country": "Brazil",       "region": "Americas",        "name": "O Globo",                      "url": "https://oglobo.globo.com",                    "status": "independent",    "icij": False},
    {"id": "la_nacion",   "country": "Argentina",    "region": "Americas",        "name": "La Nación",                    "url": "https://www.lanacion.com.ar",                 "status": "independent",    "icij": True},
    {"id": "el_tiempo",   "country": "Colombia",     "region": "Americas",        "name": "El Tiempo",                    "url": "https://www.eltiempo.com",                    "status": "independent",    "icij": False},
    {"id": "el_mercurio", "country": "Chile",        "region": "Americas",        "name": "El Mercurio",                  "url": "https://www.emol.com",                        "status": "independent",    "icij": False},
    {"id": "el_nacional", "country": "Venezuela",    "region": "Americas",        "name": "El Nacional",                  "url": "https://www.elnacional.com",                  "status": "exile",          "icij": False},
    {"id": "granma",      "country": "Cuba",         "region": "Americas",        "name": "Granma",                       "url": "http://www.granma.cu",                        "status": "state_organ",    "icij": False},
    {"id": "gleaner",     "country": "Jamaica",      "region": "Americas",        "name": "Jamaica Gleaner",              "url": "https://jamaica-gleaner.com",                 "status": "independent",    "icij": False},
    {"id": "el_comercio", "country": "Peru",         "region": "Americas",        "name": "El Comercio",                  "url": "https://elcomercio.pe",                       "status": "independent",    "icij": False},

    # ── EUROPE ────────────────────────────────────────────────────────────────
    {"id": "ft",          "country": "UK",           "region": "Europe",          "name": "Financial Times",              "url": "https://www.ft.com",                          "status": "independent",    "icij": False},
    {"id": "guardian",    "country": "UK",           "region": "Europe",          "name": "The Guardian",                 "url": "https://www.theguardian.com",                 "status": "independent",    "icij": True},
    {"id": "the_times",   "country": "UK",           "region": "Europe",          "name": "The Times",                    "url": "https://www.thetimes.co.uk",                  "status": "independent",    "icij": False},
    {"id": "irish_times", "country": "Ireland",      "region": "Europe",          "name": "Irish Times",                  "url": "https://www.irishtimes.com",                  "status": "independent",    "icij": True},
    {"id": "le_monde",    "country": "France",       "region": "Europe",          "name": "Le Monde",                     "url": "https://www.lemonde.fr",                      "status": "independent",    "icij": True},
    {"id": "le_figaro",   "country": "France",       "region": "Europe",          "name": "Le Figaro",                    "url": "https://www.lefigaro.fr",                     "status": "independent",    "icij": False},
    {"id": "faz",         "country": "Germany",      "region": "Europe",          "name": "Frankfurter Allgemeine",       "url": "https://www.faz.net",                         "status": "independent",    "icij": False},
    {"id": "sz",          "country": "Germany",      "region": "Europe",          "name": "Süddeutsche Zeitung",          "url": "https://www.sueddeutsche.de",                 "status": "independent",    "icij": True},
    {"id": "spiegel",     "country": "Germany",      "region": "Europe",          "name": "Der Spiegel",                  "url": "https://www.spiegel.de",                      "status": "independent",    "icij": True},
    {"id": "nzz",         "country": "Switzerland",  "region": "Europe",          "name": "Neue Zürcher Zeitung",         "url": "https://www.nzz.ch",                          "status": "independent",    "icij": False},
    {"id": "nrc",         "country": "Netherlands",  "region": "Europe",          "name": "NRC",                          "url": "https://www.nrc.nl",                          "status": "independent",    "icij": True},
    {"id": "volkskrant",  "country": "Netherlands",  "region": "Europe",          "name": "de Volkskrant",                "url": "https://www.volkskrant.nl",                   "status": "independent",    "icij": False},
    {"id": "standaard",   "country": "Belgium",      "region": "Europe",          "name": "De Standaard",                 "url": "https://www.standaard.be",                    "status": "independent",    "icij": False},
    {"id": "der_standard","country": "Austria",      "region": "Europe",          "name": "Der Standard",                 "url": "https://www.derstandard.at",                  "status": "independent",    "icij": False},
    {"id": "el_pais",     "country": "Spain",        "region": "Europe",          "name": "El País",                      "url": "https://elpais.com",                          "status": "independent",    "icij": True},
    {"id": "el_mundo",    "country": "Spain",        "region": "Europe",          "name": "El Mundo",                     "url": "https://www.elmundo.es",                      "status": "independent",    "icij": False},
    {"id": "corriere",    "country": "Italy",        "region": "Europe",          "name": "Corriere della Sera",          "url": "https://www.corriere.it",                     "status": "independent",    "icij": True},
    {"id": "repubblica",  "country": "Italy",        "region": "Europe",          "name": "La Repubblica",                "url": "https://www.repubblica.it",                   "status": "independent",    "icij": True},
    {"id": "publico",     "country": "Portugal",     "region": "Europe",          "name": "Público",                      "url": "https://www.publico.pt",                      "status": "independent",    "icij": False},
    {"id": "dn_sweden",   "country": "Sweden",       "region": "Europe",          "name": "Dagens Nyheter",               "url": "https://www.dn.se",                           "status": "independent",    "icij": True},
    {"id": "aftenposten", "country": "Norway",       "region": "Europe",          "name": "Aftenposten",                  "url": "https://www.aftenposten.no",                  "status": "independent",    "icij": True},
    {"id": "politiken",   "country": "Denmark",      "region": "Europe",          "name": "Politiken",                    "url": "https://politiken.dk",                        "status": "independent",    "icij": True},
    {"id": "hs",          "country": "Finland",      "region": "Europe",          "name": "Helsingin Sanomat",            "url": "https://www.hs.fi",                           "status": "independent",    "icij": False},
    {"id": "gazeta",      "country": "Poland",       "region": "Europe",          "name": "Gazeta Wyborcza",              "url": "https://wyborcza.pl",                         "status": "independent",    "icij": True},
    {"id": "hn_czech",    "country": "Czech Republic","region": "Europe",         "name": "Hospodářské noviny",           "url": "https://ihned.cz",                            "status": "independent",    "icij": False},
    {"id": "telex",       "country": "Hungary",      "region": "Europe",          "name": "Telex",                        "url": "https://telex.hu",                            "status": "independent",    "icij": False},
    {"id": "kathimerini", "country": "Greece",       "region": "Europe",          "name": "Kathimerini",                  "url": "https://www.ekathimerini.com",                "status": "independent",    "icij": False},
    {"id": "cumhuriyet",  "country": "Turkey",       "region": "Europe",          "name": "Cumhuriyet",                   "url": "https://www.cumhuriyet.com.tr",               "status": "pressured",      "icij": False},
    {"id": "hurriyet",    "country": "Turkey",       "region": "Europe",          "name": "Hürriyet",                     "url": "https://www.hurriyet.com.tr",                 "status": "state_adjacent", "icij": False},
    {"id": "kyiv_ind",    "country": "Ukraine",      "region": "Europe",          "name": "Kyiv Independent",             "url": "https://kyivindependent.com",                 "status": "independent",    "icij": False},
    {"id": "meduza",      "country": "Russia",       "region": "Europe",          "name": "Meduza",                       "url": "https://meduza.io",                           "status": "exile",          "icij": False},

    # ── AFRICA & MIDDLE EAST ──────────────────────────────────────────────────
    {"id": "daily_mav",   "country": "South Africa", "region": "Africa & Middle East", "name": "Daily Maverick",          "url": "https://www.dailymaverick.co.za",             "status": "independent",    "icij": True},
    {"id": "mg_sa",       "country": "South Africa", "region": "Africa & Middle East", "name": "Mail & Guardian",         "url": "https://mg.co.za",                            "status": "independent",    "icij": False},
    {"id": "punch_ng",    "country": "Nigeria",      "region": "Africa & Middle East", "name": "The Punch",               "url": "https://punchng.com",                         "status": "independent",    "icij": False},
    {"id": "daily_nation","country": "Kenya",        "region": "Africa & Middle East", "name": "Daily Nation",            "url": "https://nation.africa",                       "status": "independent",    "icij": False},
    {"id": "graphic_gh",  "country": "Ghana",        "region": "Africa & Middle East", "name": "Graphic Online",          "url": "https://www.graphic.com.gh",                  "status": "independent",    "icij": False},
    {"id": "reporter_et", "country": "Ethiopia",     "region": "Africa & Middle East", "name": "The Reporter Ethiopia",   "url": "https://www.thereporterethiopia.com",         "status": "independent",    "icij": False},
    {"id": "ahram",       "country": "Egypt",        "region": "Africa & Middle East", "name": "Al-Ahram",                "url": "https://english.ahram.org.eg",                "status": "state_affiliated","icij": False},
    {"id": "le_matin",    "country": "Morocco",      "region": "Africa & Middle East", "name": "Le Matin",                "url": "https://lematin.ma",                          "status": "state_affiliated","icij": False},
    {"id": "haaretz",     "country": "Israel",       "region": "Africa & Middle East", "name": "Haaretz",                 "url": "https://www.haaretz.com",                     "status": "independent",    "icij": True},
    {"id": "jpost",       "country": "Israel",       "region": "Africa & Middle East", "name": "Jerusalem Post",          "url": "https://www.jpost.com",                       "status": "independent",    "icij": False},
    {"id": "lorient",     "country": "Lebanon",      "region": "Africa & Middle East", "name": "L'Orient Today",          "url": "https://today.lorientlejour.com",             "status": "independent",    "icij": False},
    {"id": "the_national","country": "UAE",          "region": "Africa & Middle East", "name": "The National",            "url": "https://www.thenationalnews.com",             "status": "state_adjacent", "icij": False},
    {"id": "arab_news",   "country": "Saudi Arabia", "region": "Africa & Middle East", "name": "Arab News",               "url": "https://www.arabnews.com",                    "status": "state_affiliated","icij": False},
    {"id": "iran_intl",   "country": "Iran",         "region": "Africa & Middle East", "name": "Iran International",      "url": "https://www.iranintl.com/en",                 "status": "exile",          "icij": False},
    {"id": "jordan_times","country": "Jordan",       "region": "Africa & Middle East", "name": "Jordan Times",            "url": "https://www.jordantimes.com",                 "status": "state_adjacent", "icij": False},

    # ── ASIA & PACIFIC ────────────────────────────────────────────────────────
    {"id": "scmp",        "country": "Hong Kong",    "region": "Asia & Pacific",  "name": "South China Morning Post",     "url": "https://www.scmp.com",                        "status": "state_adjacent", "icij": False},
    {"id": "peoples_d",   "country": "China",        "region": "Asia & Pacific",  "name": "People's Daily",               "url": "http://en.people.cn",                         "status": "state_organ",    "icij": False},
    {"id": "global_times","country": "China",        "region": "Asia & Pacific",  "name": "Global Times",                 "url": "https://www.globaltimes.cn",                  "status": "state_organ",    "icij": False},
    {"id": "taipei_times","country": "Taiwan",       "region": "Asia & Pacific",  "name": "Taipei Times",                 "url": "https://www.taipeitimes.com",                 "status": "independent",    "icij": False},
    {"id": "japan_times", "country": "Japan",        "region": "Asia & Pacific",  "name": "Japan Times",                  "url": "https://www.japantimes.co.jp",                "status": "independent",    "icij": False},
    {"id": "asahi",       "country": "Japan",        "region": "Asia & Pacific",  "name": "Asahi Shimbun",                "url": "https://www.asahi.com/ajw",                   "status": "independent",    "icij": True},
    {"id": "joongang",    "country": "South Korea",  "region": "Asia & Pacific",  "name": "Korea JoongAng Daily",         "url": "https://koreajoongangdaily.joins.com",        "status": "independent",    "icij": False},
    {"id": "kr_herald",   "country": "South Korea",  "region": "Asia & Pacific",  "name": "The Korea Herald",             "url": "https://www.koreaherald.com",                 "status": "independent",    "icij": False},
    {"id": "the_hindu",   "country": "India",        "region": "Asia & Pacific",  "name": "The Hindu",                    "url": "https://www.thehindu.com",                    "status": "independent",    "icij": True},
    {"id": "ind_express", "country": "India",        "region": "Asia & Pacific",  "name": "Indian Express",               "url": "https://indianexpress.com",                   "status": "independent",    "icij": True},
    {"id": "hind_times",  "country": "India",        "region": "Asia & Pacific",  "name": "Hindustan Times",              "url": "https://www.hindustantimes.com",              "status": "independent",    "icij": False},
    {"id": "dawn",        "country": "Pakistan",     "region": "Asia & Pacific",  "name": "Dawn",                         "url": "https://www.dawn.com",                        "status": "independent",    "icij": True},
    {"id": "daily_star",  "country": "Bangladesh",   "region": "Asia & Pacific",  "name": "The Daily Star",               "url": "https://www.thedailystar.net",                "status": "independent",    "icij": False},
    {"id": "straits_t",   "country": "Singapore",    "region": "Asia & Pacific",  "name": "The Straits Times",            "url": "https://www.straitstimes.com",                "status": "state_adjacent", "icij": False},
    {"id": "bkk_post",    "country": "Thailand",     "region": "Asia & Pacific",  "name": "Bangkok Post",                 "url": "https://www.bangkokpost.com",                 "status": "independent",    "icij": False},
    {"id": "inquirer",    "country": "Philippines",  "region": "Asia & Pacific",  "name": "Philippine Daily Inquirer",    "url": "https://inquirer.net",                        "status": "independent",    "icij": True},
    {"id": "jakarta_post","country": "Indonesia",    "region": "Asia & Pacific",  "name": "The Jakarta Post",             "url": "https://www.thejakartapost.com",              "status": "independent",    "icij": False},
    {"id": "malaysiakini","country": "Malaysia",     "region": "Asia & Pacific",  "name": "Malaysiakini",                 "url": "https://www.malaysiakini.com",                "status": "independent",    "icij": True},
    {"id": "irrawaddy",   "country": "Myanmar",      "region": "Asia & Pacific",  "name": "The Irrawaddy",                "url": "https://www.irrawaddy.com",                   "status": "exile",          "icij": False},
    {"id": "smh",         "country": "Australia",    "region": "Asia & Pacific",  "name": "Sydney Morning Herald",        "url": "https://www.smh.com.au",                      "status": "independent",    "icij": True},
    {"id": "the_aus",     "country": "Australia",    "region": "Asia & Pacific",  "name": "The Australian",               "url": "https://www.theaustralian.com.au",            "status": "independent",    "icij": False},
    {"id": "nz_herald",   "country": "New Zealand",  "region": "Asia & Pacific",  "name": "NZ Herald",                    "url": "https://www.nzherald.co.nz",                  "status": "independent",    "icij": False},
]

# Sources used as global baseline — excluded from newsletter output
# but used to calibrate what's already globally known
BASELINE_SOURCES = {"nyt", "wsj", "wapo", "guardian", "ft"}

# Status label copy for Substack output
STATUS_LABELS = {
    "state_organ":    "⚠️ State organ — editorial content reflects official government position",
    "state_affiliated": "⚠️ State-affiliated publication",
    "state_adjacent": "⚠️ State-adjacent — editorially professional, operates in proximity to government",
    "exile":          "⚠️ Exile publication — independent journalism published outside home country",
    "pressured":      "⚠️ Independent but operating under documented government pressure",
}

def get_sources(exclude_baseline=True):
    if exclude_baseline:
        return [s for s in SOURCES if s["id"] not in BASELINE_SOURCES]
    return SOURCES

def get_baseline_sources():
    return [s for s in SOURCES if s["id"] in BASELINE_SOURCES]
