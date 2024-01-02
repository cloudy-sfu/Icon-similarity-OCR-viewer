select i.image_idx, b.box_idx, t.language_code, t.confidence, t.content,
       b.left_upper_x, b.left_upper_y, b.right_upper_x, b.right_upper_y,
       b.right_lower_x, b.right_lower_y, b.left_lower_x, b.left_lower_y
from images i join ocr_boxes b on i.case_idx = b.case_idx and i.image_idx = b.image_idx
     join ocr_texts t on b.case_idx = t.case_idx and b.image_idx = t.image_idx
                             and b.box_idx = t.box_idx
where i.case_idx = %s and t.language_code in %s
