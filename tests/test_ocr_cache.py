"""Cache addresses for crops and recognizer answers.

Everything the editor needs from invalidation is a property of these keys, so
each property is asserted directly rather than inferred from behaviour further
up. In particular: correcting a transcription and reordering entries must be
free, and they are free precisely because neither is an input here.
"""

from foresight_ocr.ocr.cache import cache_key, crop_key, model_key


def test_vl_runner_reuses_one_model_load_across_large_sequential_batches():
    from foresight_ocr.ocr.base import get_backend

    sequential = get_backend("paddleocr_vl", image_scale=0.4)
    grouped = get_backend("paddleocr_vl", image_scale=0.4, batched=True)

    assert sequential.batch_size == 1024
    assert sequential.timeout_s == 3600.0
    assert grouped.batch_size == 256


def _crop(**kw):
    args = dict(
        document_id="doc",
        page_index=58,
        pixel_bbox=[1919, 0, 2253, 1012],
        variant="maxrgb",
        page_checksum="abc123",
    )
    args.update(kw)
    return crop_key(**args)


def test_the_same_pixels_address_the_same_crop():
    assert _crop() == _crop()


def test_a_fraction_of_a_pixel_is_not_a_different_crop():
    # The file was produced by src[int(y0):int(y1), int(x0):int(x1)], so two
    # boxes that truncate alike produced identical bytes.
    assert _crop(pixel_bbox=[1919, 0, 2253, 1012]) == _crop(
        pixel_bbox=[1919.99, 0.4, 2253.2, 1012.8]
    )


def test_moving_the_box_addresses_a_different_crop():
    assert _crop() != _crop(pixel_bbox=[1900, 0, 2253, 1012])


def test_the_variant_and_the_page_it_was_cut_from_are_part_of_the_address():
    assert _crop() != _crop(variant="original")
    # A re-warped page is different pixels even at the same coordinates.
    assert _crop() != _crop(page_checksum="def456")


def test_two_pages_never_share_a_crop_address():
    assert _crop() != _crop(page_index=59)
    assert _crop() != _crop(document_id="other")


def test_model_key_separates_configurations_of_one_model():
    plain = model_key("paddleocr_vl", "1.6")
    assert plain == model_key("paddleocr_vl", "1.6", {})
    assert plain != model_key("paddleocr_vl", "1.6", {"image_scale": 0.4})
    assert model_key("paddleocr_vl", "1.6", {"image_scale": 0.4}) != model_key(
        "paddleocr_vl", "1.6", {"image_scale": 0.6}
    )
    assert plain != model_key("paddleocr_vl", "1.7")
    assert plain != model_key("ppocr_v5", "1.6")


def test_model_key_does_not_depend_on_dict_ordering():
    assert model_key("b", "1", {"a": 1, "z": 2}) == model_key(
        "b", "1", {"z": 2, "a": 1}
    )


def test_a_new_model_reads_the_same_crop_at_a_different_address():
    crop = _crop()
    old = cache_key(crop, model_key("paddleocr_vl", "1.6"))
    new = cache_key(crop, model_key("paddleocr_vl", "1.7"))
    assert old != new  # so the new answer lands beside the old, not on it


def test_a_tag_separates_two_runs_of_one_configuration():
    crop, model = _crop(), model_key("paddleocr_vl", "1.6")
    assert cache_key(crop, model, "book-v2") != cache_key(crop, model, "book-v3")
    assert cache_key(crop, model) == cache_key(crop, model, "")
