from __future__ import annotations

import copy
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from bedside_audio.catalog import InvalidMediaPath, MediaError, MediaNotFound
from bedside_audio.config import DlnaSettings
from bedside_audio.dlna_catalog import DlnaCatalog
from bedside_audio.player import PlayerError


SOURCE_ID = "plex_uuid"
ENTITY_ID = "media_player.bedroom_tv"
ROOT_ID = f"media-source://dlna_dms/{SOURCE_ID}/:0"
FOLDER_TYPE = "object.container.storageFolder"


def folder(
    name: str, object_id: str, *, media_type: str = FOLDER_TYPE
) -> dict[str, object]:
    return {
        "media_content_id": f"media-source://dlna_dms/{SOURCE_ID}/:{object_id}",
        "media_content_type": media_type,
        "title": name,
        "media_class": "directory",
        "can_expand": True,
        "can_play": False,
    }


def video(
    name: str,
    object_id: str,
    *,
    media_type: str = "video/x-matroska",
    media_class: str = "video",
) -> dict[str, object]:
    return {
        "media_content_id": f"media-source://dlna_dms/{SOURCE_ID}/:{object_id}",
        "media_content_type": media_type,
        "title": name,
        "media_class": media_class,
        "can_expand": False,
        "can_play": True,
    }


def response(
    item: dict[str, object],
    children: list[dict[str, object]],
    *,
    not_shown: int = 0,
) -> dict[str, object]:
    return {**item, "children": children, "not_shown": not_shown}


class Browser:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def browse_media(
        self, entity_id: str, media_content_id: str, media_content_type: str
    ) -> dict[str, object]:
        self.calls.append((entity_id, media_content_id, media_content_type))
        return copy.deepcopy(self.responses[media_content_id])


def make_catalog(
    browser: Browser,
    allowed_folders: tuple[tuple[str, ...], ...] = (),
    excluded_title_patterns: tuple[str, ...] = (),
) -> DlnaCatalog:
    return DlnaCatalog(
        DlnaSettings(
            source_id=SOURCE_ID,
            browse_player_entity_id=ENTITY_ID,
            owner_user_id="owner",
            allowed_folders=allowed_folders,
            excluded_title_patterns=excluded_title_patterns,
        ),
        browser,
    )


def test_root_browse_exposes_only_video_and_folder_handles_to_browser() -> None:
    movie = video("Film", "0$1")
    movie["url"] = "https://private-server/video?token=secret"
    movie["thumbnail"] = "http://private-server/cover?token=secret"
    series = folder("Series", "0$2")
    song = {
        **video("Song", "0$3"),
        "media_content_type": "audio/mpeg",
        "media_class": "music",
    }
    browser = Browser(
        {ROOT_ID: response(folder("Library", "0"), [series, movie, song])}
    )
    catalog = make_catalog(browser)

    assert catalog.health() is None
    assert catalog.health("") is None
    assert browser.calls == []

    listing = catalog.list_dir()

    assert listing["path"] == ""
    assert listing["parent"] is None
    assert listing["folder_queue"] is True
    assert "playlist" not in listing
    assert [(entry["name"], entry["kind"]) for entry in listing["entries"]] == [
        ("Series", "folder"),
        ("Film", "file"),
    ]
    for entry in listing["entries"]:
        assert set(entry) == {"path", "name", "kind"}
        assert re.fullmatch(r"[A-Za-z0-9_-]+", entry["path"])
        assert entry["path"] != ROOT_ID
    exposed = json.dumps(listing)
    assert all(
        item["media_content_id"] not in exposed for item in (movie, series, song)
    )
    assert "media-source://" not in exposed
    assert "private-server" not in exposed
    assert "secret" not in exposed
    assert browser.calls == [(ENTITY_ID, ROOT_ID, FOLDER_TYPE)]


def test_library_allowlist_hides_siblings_and_blocks_configured_queue_bypass() -> None:
    allowed = folder("Allowed Show", "0$allowed")
    hidden = folder("Hidden Show", "0$hidden")
    allowed_episode = video("Allowed Episode", "0$allowed$1")
    hidden_episode = video("Hidden Episode", "0$hidden$1")
    browser = Browser({
        ROOT_ID: response(folder("Library", "0"), [allowed, hidden]),
        str(allowed["media_content_id"]): response(allowed, [allowed_episode]),
        str(hidden["media_content_id"]): response(hidden, [hidden_episode]),
    })
    catalog = make_catalog(browser, (("Allowed Show",),))

    listing = catalog.list_dir()

    assert [entry["name"] for entry in listing["entries"]] == ["Allowed Show"]
    assert len(catalog.configured_queue((("Allowed Show",),))) == 1
    with pytest.raises(MediaNotFound, match="unavailable"):
        catalog.configured_queue((("Hidden Show",),))


def test_library_exclusion_globs_hide_matching_titles_and_folder_subtrees() -> None:
    specials = folder("Specials", "0$specials")
    season_zero = folder("Season 0", "0$season0")
    season_one = folder("Season 1", "0$season1")
    preview = video("Season 01 Preview", "0$preview")
    browser = Browser({
        ROOT_ID: response(
            folder("Library", "0"),
            [specials, season_zero, season_one, preview],
        ),
    })
    catalog = make_catalog(
        browser,
        excluded_title_patterns=("specials", "Season 0*"),
    )

    listing = catalog.list_dir()

    assert [entry["name"] for entry in listing["entries"]] == ["Season 1"]
    with pytest.raises(MediaNotFound, match="unavailable"):
        catalog.configured_queue((("Specials",),))


def test_mixed_dlna_video_classes_remain_playable_without_relaxing_mime_or_ids() -> None:
    children = [
        video("Episode", "0$1", media_class="episode"),
        video("Movie", "0$2", media_class="movie"),
        video("Show", "0$3", media_class="tv_show"),
        {
            **video("Music", "0$4", media_class="music"),
            "media_content_type": "audio/mpeg",
        },
    ]
    browser = Browser({ROOT_ID: response(folder("Video", "0"), children)})
    catalog = make_catalog(browser)

    listing = catalog.list_dir()

    assert listing["folder_queue"] is True
    assert [entry["name"] for entry in listing["entries"]] == [
        "Episode", "Movie", "Show",
    ]
    assert all(entry["kind"] == "file" for entry in listing["entries"])
    movie, queue, index = catalog.prepare(listing["entries"][1]["path"])
    assert movie.media_content_id == children[1]["media_content_id"]
    assert movie.media_content_type == "video/x-matroska"
    assert queue == [entry["path"] for entry in listing["entries"]]
    assert index == 1
    assert "media-source://" not in json.dumps(listing)
    assert all(call == (ENTITY_ID, ROOT_ID, FOLDER_TYPE) for call in browser.calls)


def test_nested_browse_uses_registered_folder_and_preserves_its_parent_handle() -> None:
    series = folder(
        "Series", "0$2", media_type="object.container.album.videoAlbum"
    )
    episode = video(
        "Episode 1", "0$2$1", media_type="video/mp4", media_class="episode"
    )
    cover = {
        **video("Cover", "0$2$2"),
        "media_content_type": "image/jpeg",
        "media_class": "image",
    }
    series_id = str(series["media_content_id"])
    browser = Browser(
        {
            ROOT_ID: response(folder("Library", "0"), [series]),
            series_id: response(series, [episode, cover]),
        }
    )
    catalog = make_catalog(browser)
    series_handle = catalog.list_dir()["entries"][0]["path"]

    assert catalog.health(series_handle) is None
    assert browser.calls == [(ENTITY_ID, ROOT_ID, FOLDER_TYPE)]

    listing = catalog.list_dir(series_handle)

    assert listing["path"] == series_handle
    assert listing["parent"] == ""
    assert listing["display_path"] == "DLNA / Series"
    assert listing["folder_queue"] is True
    assert [(entry["name"], entry["kind"]) for entry in listing["entries"]] == [
        ("Episode 1", "file")
    ]
    assert series_handle not in listing["entries"][0]["path"]
    assert series_id not in json.dumps(listing)
    assert browser.calls[-1] == (
        ENTITY_ID,
        series_id,
        "object.container.album.videoAlbum",
    )


def test_prepare_rebrowses_parent_and_keeps_duplicate_file_occurrences_in_queue(
) -> None:
    first = video("Repeat", "0$1")
    second = video("Second", "0$2", media_type="video/mp4")
    browser = Browser(
        {
            ROOT_ID: response(
                folder("Library", "0"),
                [first, folder("Extras", "0$extras"), second, first],
            )
        }
    )
    catalog = make_catalog(browser)
    listing = catalog.list_dir()
    file_handles = [
        entry["path"] for entry in listing["entries"] if entry["kind"] == "file"
    ]

    selected, queue, index = catalog.prepare(file_handles[2])

    assert selected.media_content_id == first["media_content_id"]
    assert selected.media_content_type == "video/x-matroska"
    assert selected.name == "Repeat"
    assert selected.display_path == "DLNA / Repeat"
    assert first["media_content_id"] not in repr(selected)
    assert index == 2
    assert len(queue) == 3
    assert len(set(queue)) == 3
    assert queue[index] == file_handles[2]
    assert [catalog.file(path).media_content_id for path in queue] == [
        first["media_content_id"],
        second["media_content_id"],
        first["media_content_id"],
    ]
    assert all(
        call == (ENTITY_ID, ROOT_ID, FOLDER_TYPE) for call in browser.calls
    )


def test_configured_queue_resolves_files_and_folders_in_explicit_order() -> None:
    show = folder("Example Show", "0$show")
    season = folder("Season 2", "0$show$season2")
    second = video("Episode 2", "0$show$season2$2", media_class="episode")
    first = video("Episode 1", "0$show$season2$1", media_class="episode")
    movie = video("Example Movie", "0$movie", media_class="movie")
    browser = Browser({
        ROOT_ID: response(folder("Library", "0"), [show, movie]),
        str(show["media_content_id"]): response(show, [season]),
        str(season["media_content_id"]): response(season, [second, first]),
    })
    catalog = make_catalog(browser)

    queue = catalog.configured_queue((
        ("Example Show", "Season 2"),
        ("Example Movie",),
        ("Example Movie",),
    ))

    assert [catalog.file(path).media_content_id for path in queue] == [
        second["media_content_id"],
        first["media_content_id"],
        movie["media_content_id"],
        movie["media_content_id"],
    ]


def test_selected_folder_queue_recurses_without_starting_playback() -> None:
    show = folder("Example Show", "0$show")
    season = folder("Season 1", "0$show$season1")
    episode = video("Episode", "0$show$season1$1", media_class="episode")
    browser = Browser({
        ROOT_ID: response(folder("Library", "0"), [show]),
        str(show["media_content_id"]): response(show, [season]),
        str(season["media_content_id"]): response(season, [episode]),
    })
    catalog = make_catalog(browser)
    show_handle = catalog.list_dir()["entries"][0]["path"]

    queue, display_path = catalog.folder_queue(show_handle)

    assert display_path == "DLNA / Example Show"
    assert [catalog.file(path).media_content_id for path in queue] == [
        episode["media_content_id"],
    ]


def test_configured_queue_rejects_missing_ambiguous_and_empty_items() -> None:
    first = folder("Same name", "0$first")
    second = folder("Same name", "0$second")
    empty = folder("Empty", "0$empty")
    browser = Browser({
        ROOT_ID: response(folder("Library", "0"), [first, second, empty]),
        str(empty["media_content_id"]): response(empty, []),
    })
    catalog = make_catalog(browser)

    with pytest.raises(MediaNotFound, match="unavailable"):
        catalog.configured_queue((("Missing",),))
    with pytest.raises(MediaNotFound, match="ambiguous"):
        catalog.configured_queue((("Same name",),))
    with pytest.raises(MediaNotFound, match="no playable"):
        catalog.configured_queue((("Empty",),))


def test_configured_queue_rejects_more_than_256_resolved_videos() -> None:
    first = folder("First", "0$first")
    second = folder("Second", "0$second")
    first_videos = [
        video(f"First {index}", f"0$first${index}") for index in range(200)
    ]
    second_videos = [
        video(f"Second {index}", f"0$second${index}") for index in range(57)
    ]
    browser = Browser({
        ROOT_ID: response(folder("Library", "0"), [first, second]),
        str(first["media_content_id"]): response(first, first_videos),
        str(second["media_content_id"]): response(second, second_videos),
    })

    with pytest.raises(MediaError, match="at most 256"):
        make_catalog(browser).configured_queue((("First",), ("Second",)))


def test_queue_rebrowses_parent_and_reflects_changed_siblings() -> None:
    old = video("Old", "0$old")
    selected = video("Selected", "0$selected")
    browser = Browser({ROOT_ID: response(folder("Library", "0"), [old, selected])})
    catalog = make_catalog(browser)
    selected_handle = catalog.list_dir()["entries"][1]["path"]
    replacement = video("New", "0$new", media_type="video/mp4")
    browser.responses[ROOT_ID] = response(
        folder("Library", "0"), [replacement, selected]
    )

    queue, index = catalog.queue(selected_handle)

    assert index == 1
    assert queue[index] == selected_handle
    assert [catalog.file(path).name for path in queue] == ["New", "Selected"]
    assert browser.calls[1] == (ENTITY_ID, ROOT_ID, FOLDER_TYPE)


@pytest.mark.parametrize(
    "changed_children",
    [
        [video("First", "0$first"), video("Selected", "0$replacement")],
        [
            video("First", "0$first"),
            video("Selected", "0$selected", media_type="video/mp4"),
        ],
        [video("First", "0$first"), video("Renamed", "0$selected")],
        [video("Selected", "0$selected"), video("First", "0$first")],
        [video("First", "0$first")],
    ],
)
def test_stale_file_handle_cannot_prepare_play_or_queue_changed_item(
    changed_children: list[dict[str, object]],
) -> None:
    first = video("First", "0$first")
    selected = video("Selected", "0$selected")
    browser = Browser({ROOT_ID: response(folder("Library", "0"), [first, selected])})
    catalog = make_catalog(browser)
    selected_handle = catalog.list_dir()["entries"][1]["path"]
    browser.responses[ROOT_ID] = response(folder("Library", "0"), changed_children)

    for action in (catalog.prepare, catalog.file, catalog.queue):
        with pytest.raises(MediaNotFound, match="no longer available"):
            action(selected_handle)

    assert browser.calls == [(ENTITY_ID, ROOT_ID, FOLDER_TYPE)] * 4


def test_nested_file_replays_only_after_its_registered_parent_is_rebrowsed() -> None:
    series = folder("Series", "0$series")
    episode = video("Episode", "0$series$episode", media_class="episode")
    series_id = str(series["media_content_id"])
    browser = Browser(
        {
            ROOT_ID: response(folder("Library", "0"), [series]),
            series_id: response(series, [episode]),
        }
    )
    catalog = make_catalog(browser)
    series_handle = catalog.list_dir()["entries"][0]["path"]
    episode_handle = catalog.list_dir(series_handle)["entries"][0]["path"]

    selected, queue, index = catalog.prepare(episode_handle)

    assert selected.media_content_id == episode["media_content_id"]
    assert queue == [episode_handle]
    assert index == 0
    assert browser.calls[-1] == (ENTITY_ID, series_id, FOLDER_TYPE)

    browser.responses[series_id] = response(series, [])
    with pytest.raises(MediaNotFound):
        catalog.file(episode_handle)
    assert browser.calls[-1] == (ENTITY_ID, series_id, FOLDER_TYPE)


def test_prepare_and_skip_reuse_listing_handles_for_unchanged_season() -> None:
    season = folder("Season 1", "0$season")
    first = video("Episode 1", "0$episode1", media_class="episode")
    second = video("Episode 2", "0$episode2", media_class="episode")
    season_id = str(season["media_content_id"])
    browser = Browser(
        {
            ROOT_ID: response(folder("Library", "0"), [season]),
            season_id: response(season, [first, second, first]),
        }
    )
    catalog = make_catalog(browser)
    season_handle = catalog.list_dir()["entries"][0]["path"]
    listing = catalog.list_dir(season_handle)
    listed_paths = [entry["path"] for entry in listing["entries"]]

    _, queue, index = catalog.prepare(listed_paths[0])
    next_queue, next_index = catalog.queue(listed_paths[1])
    refreshed = catalog.list_dir(season_handle)

    assert index == 0
    assert next_index == 1
    assert queue == next_queue == listed_paths
    assert [entry["path"] for entry in refreshed["entries"]] == listed_paths
    assert len(set(listed_paths)) == 3
    assert all(call[0] == ENTITY_ID for call in browser.calls)


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "media-source://dlna_dms/other/:0$1",
        f"media-source://dlna_dms/{SOURCE_ID}.evil/:0$1",
        "https://private-server/video?token=secret",
        f"media-source://dlna_dms/{SOURCE_ID}/:",
        f"media-source://dlna_dms/{SOURCE_ID}/:..",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1/../2",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1%2Fsecret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1%252fsecret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1%5csecret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1?token=secret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1#fragment",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1\\secret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0$1\x00secret",
    ],
)
def test_browse_rejects_untrusted_child_uris_without_issuing_more_core_calls(
    unsafe_id: str,
) -> None:
    poisoned = {**video("Poisoned", "0$1"), "media_content_id": unsafe_id}
    browser = Browser(
        {ROOT_ID: response(folder("Library", "0"), [video("Good", "0$good"), poisoned])}
    )
    catalog = make_catalog(browser)

    with pytest.raises(MediaError) as error:
        catalog.list_dir()

    assert unsafe_id not in str(error.value)
    assert browser.calls == [(ENTITY_ID, ROOT_ID, FOLDER_TYPE)]


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "media-source://dlna_dms/other/:0$1",
        f"media-source://dlna_dms/{SOURCE_ID}/:0%2fsecret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0/secret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0?token=secret",
        f"media-source://dlna_dms/{SOURCE_ID}/:0..secret",
    ],
)
def test_unsupported_child_identifier_has_explicit_safe_error_without_second_browse(
    unsafe_id: str,
) -> None:
    poisoned = {**video("Poisoned", "0$1"), "media_content_id": unsafe_id}
    browser = Browser(
        {ROOT_ID: response(folder("Library", "0"), [video("Good", "0$good"), poisoned])}
    )

    with pytest.raises(
        MediaError, match="^Unsupported DLNA media identifier$"
    ) as error:
        make_catalog(browser).list_dir()

    assert unsafe_id not in str(error.value)
    assert browser.calls == [(ENTITY_ID, ROOT_ID, FOLDER_TYPE)]


@pytest.mark.parametrize(
    "changes",
    [
        {"media_content_type": "video/mp4?token=secret"},
        {"media_content_type": "application/octet-stream"},
        {"media_content_type": "audio/mpeg"},
        {"media_class": "music"},
        {"can_expand": True},
        {"title": "https://private-server/video?token=secret"},
        {"title": "Film x-plex-token=secret"},
        {"title": "\\\\server\\private\\video"},
        {"title": "Film\nInjected"},
    ],
)
def test_browse_rejects_untrusted_playable_metadata(changes: dict[str, object]) -> None:
    child = {**video("Film", "0$1"), **changes}
    browser = Browser({ROOT_ID: response(folder("Library", "0"), [child])})

    with pytest.raises(MediaError) as error:
        make_catalog(browser).list_dir()

    assert "secret" not in str(error.value)


def test_normal_punctuation_in_video_titles_is_not_treated_as_a_url() -> None:
    titles = ["Who?", "100% Real", "A/B", "A = B"]
    browser = Browser(
        {
            ROOT_ID: response(
                folder("Library", "0"),
                [video(name, f"0${index}") for index, name in enumerate(titles)],
            )
        }
    )

    listing = make_catalog(browser).list_dir()

    assert [entry["name"] for entry in listing["entries"]] == titles


@pytest.mark.parametrize(
    "changes",
    [
        {"media_content_id": "media-source://dlna_dms/other/:0"},
        {"media_content_type": "object.container"},
        {"can_expand": False},
        {"can_play": True},
        {"can_play": "false"},
        {"can_play": None},
    ],
)
def test_root_browse_rejects_forged_id_type_or_folder_flags(
    changes: dict[str, object],
) -> None:
    root = {**folder("Library", "0"), **changes}
    browser = Browser({ROOT_ID: response(root, [video("Film", "0$1")])})

    with pytest.raises(MediaError):
        make_catalog(browser).list_dir()

    assert browser.calls == [(ENTITY_ID, ROOT_ID, FOLDER_TYPE)]


def test_empty_folder_does_not_offer_a_queue() -> None:
    browser = Browser({ROOT_ID: response(folder("Library", "0"), [])})

    listing = make_catalog(browser).list_dir()

    assert listing["entries"] == []
    assert listing["folder_queue"] is False


@pytest.mark.parametrize("not_shown", [1, 100, -1, True, "1"])
def test_incomplete_browse_never_presents_a_partial_folder_or_queue(
    not_shown: object,
) -> None:
    child = video("Film", "0$1")
    browser = Browser({ROOT_ID: response(folder("Library", "0"), [child])})
    catalog = make_catalog(browser)
    handle = catalog.list_dir()["entries"][0]["path"]
    browser.responses[ROOT_ID]["not_shown"] = not_shown

    for action in (
        catalog.list_dir,
        lambda: catalog.prepare(handle),
        lambda: catalog.queue(handle),
    ):
        with pytest.raises(MediaError, match="Incomplete"):
            action()


@pytest.mark.parametrize(
    "children",
    [None, {}, "not-a-list", [video("Film", str(n)) for n in range(600)]],
)
def test_missing_bad_or_unbounded_children_fail_closed(children: object) -> None:
    browser = Browser({ROOT_ID: {**folder("Library", "0"), "children": children}})

    with pytest.raises(MediaError, match="Incomplete"):
        make_catalog(browser).list_dir()


def test_core_failure_has_safe_message_and_health_only_uses_cached_state() -> None:
    class FlakyBrowser(Browser):
        unavailable = True

        def browse_media(
            self, entity_id: str, media_content_id: str, media_content_type: str
        ) -> dict[str, object]:
            if self.unavailable:
                self.calls.append((entity_id, media_content_id, media_content_type))
                raise ConnectionError("https://private-server?token=secret")
            return super().browse_media(entity_id, media_content_id, media_content_type)

    browser = FlakyBrowser({ROOT_ID: response(folder("Library", "0"), [])})
    catalog = make_catalog(browser)

    with pytest.raises(MediaError) as error:
        catalog.list_dir()

    assert "secret" not in str(error.value)
    assert catalog.health() == "DLNA browsing unavailable"
    assert catalog.health("") == "DLNA browsing unavailable"
    assert len(browser.calls) == 1

    browser.unavailable = False
    assert catalog.list_dir()["entries"] == []
    assert catalog.health() is None
    assert len(browser.calls) == 2


def test_unexpected_browser_error_is_not_hidden_as_offline() -> None:
    class BrokenBrowser(Browser):
        def browse_media(
            self, entity_id: str, media_content_id: str, media_content_type: str
        ) -> dict[str, object]:
            raise ValueError("programming bug")

    catalog = make_catalog(BrokenBrowser({}))

    with pytest.raises(ValueError, match="programming bug"):
        catalog.list_dir()

    assert catalog.health() is None


@pytest.mark.parametrize(
    "failure",
    [
        PlayerError("Core response included token=secret"),
        ConnectionError("https://private-server/video?token=secret"),
    ],
)
def test_core_failure_logs_only_exception_class(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingBrowser(Browser):
        def browse_media(
            self, entity_id: str, media_content_id: str, media_content_type: str
        ) -> dict[str, object]:
            self.calls.append((entity_id, media_content_id, media_content_type))
            raise failure

    catalog = make_catalog(FailingBrowser({}))

    with caplog.at_level(logging.WARNING, logger="bedside_audio.dlna_catalog"):
        with pytest.raises(MediaError, match="DLNA browsing unavailable") as error:
            catalog.list_dir()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bedside_audio.dlna_catalog"
    ]
    assert messages == [f"DLNA browse failed: {type(failure).__name__}"]
    assert "secret" not in caplog.text
    assert "private-server" not in caplog.text
    assert "secret" not in str(error.value)
    assert catalog.health() == "DLNA browsing unavailable"


def test_malformed_core_response_is_reflected_by_cached_health_until_recovery() -> None:
    browser = Browser({ROOT_ID: {**folder("Library", "0"), "children": None}})
    catalog = make_catalog(browser)

    with pytest.raises(MediaError):
        catalog.list_dir()

    assert catalog.health() == "DLNA browsing unavailable"
    assert len(browser.calls) == 1
    browser.responses[ROOT_ID] = response(folder("Library", "0"), [])
    assert catalog.list_dir()["entries"] == []
    assert catalog.health() is None


def test_evicted_or_fabricated_handles_cannot_browse_or_prepare() -> None:
    folders = [folder(f"Season {number}", f"0$season{number}") for number in range(6)]
    responses = {ROOT_ID: response(folder("Library", "0"), folders)}
    for season_number, season in enumerate(folders):
        responses[str(season["media_content_id"])] = response(
            season,
            [
                video(
                    f"Film {season_number}-{number}",
                    f"0$season{season_number}${number}",
                )
                for number in range(220)
            ],
        )
    browser = Browser(responses)
    catalog = make_catalog(browser)
    for season_number in range(6):
        season_handle = catalog.list_dir()["entries"][season_number]["path"]
        file_handle = catalog.list_dir(season_handle)["entries"][0]["path"]
        if season_number == 0:
            oldest_handle = file_handle
        latest_handle = file_handle

    with pytest.raises(MediaNotFound):
        catalog.prepare(oldest_handle)
    assert catalog.prepare(latest_handle)[0].name == "Film 5-0"
    with pytest.raises(MediaNotFound):
        catalog.list_dir("A" * 16)
    for relative in ("../media", ROOT_ID, "https://example.invalid/", "A" * 17):
        with pytest.raises(InvalidMediaPath):
            catalog.list_dir(relative)
    assert all(call[0] == ENTITY_ID for call in browser.calls)


def test_concurrent_eviction_during_browse_does_not_return_stale_folder() -> None:
    series = folder("Series", "0$series")
    series_id = str(series["media_content_id"])
    entered = Event()
    resume = Event()

    class BlockingBrowser(Browser):
        def browse_media(
            self, entity_id: str, media_content_id: str, media_content_type: str
        ) -> dict[str, object]:
            if media_content_id == series_id:
                entered.set()
                assert resume.wait(timeout=5)
            return super().browse_media(entity_id, media_content_id, media_content_type)

    browser = BlockingBrowser(
        {
            ROOT_ID: response(folder("Library", "0"), [series]),
            series_id: response(series, []),
        }
    )
    catalog = make_catalog(browser)
    series_handle = catalog.list_dir()["entries"][0]["path"]
    browser.responses[ROOT_ID] = response(
        folder("Library", "0"), [video(f"Film {n}", f"0${n}") for n in range(220)]
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        browsing = executor.submit(catalog.list_dir, series_handle)
        try:
            assert entered.wait(timeout=5)
            for _ in range(5):
                catalog.list_dir()
        finally:
            resume.set()
        with pytest.raises(MediaNotFound):
            browsing.result(timeout=5)


def test_handle_collision_fails_closed_instead_of_looping_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def same_handle(_: int) -> str:
        nonlocal attempts
        attempts += 1
        if attempts > 12:
            raise AssertionError("Handle generation was not bounded")
        return "A" * 16

    monkeypatch.setattr("bedside_audio.dlna_catalog.secrets.token_urlsafe", same_handle)
    browser = Browser(
        {
            ROOT_ID: response(
                folder("Library", "0"), [video("A", "0$a"), video("B", "0$b")]
            )
        }
    )

    with pytest.raises(MediaError, match="handle"):
        make_catalog(browser).list_dir()
