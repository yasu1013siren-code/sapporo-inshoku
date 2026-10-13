# 函館のホットペッパー掲載照合

札幌10区は既存の掲載条件のまま。函館は店舗ページの掲載を確認できない店のみ表示する。

collector_ver72.py / collector_ver72_hakodate.py のJSON出力直前に照合を実行する。collector_ver7380.py は函館対応のcoreを呼ぶため同じ選定を通る。run_and_publish.bat は照合記録と保留一覧も保存する。

ホットペッパー函館検索で FWT に店舗名・表記違い・確認済み住所を指定。入力値の反映と検索件数を検証し、全検索0件の場合に限り「確認日時点で掲載なし」とする。候補あり、住所根拠不足、検索失敗は保留。確認済み店舗ページは除外する。

hotpepper_reviews.json に店舗名、元データ住所、照合状態、確認日時、根拠URL、住所・別名の確認を保存。確認日時はタイムゾーン付き。正常照合は1日再利用し、7日経過で掲載資格失効。1回30候補まで照合、残りは保留。掲載ありの再確認では保存済み店舗ページの店名と住所を照合する。HTML変更・通信失敗では未掲載扱いにしない。

hakodate_pending.json は保留・掲載ありで除外した情報の記録。DBを削除しない。公開画面にも7日以内のnot_found照合を必須とし、旧データや静的fallbackの未確認函館店を表示しない。

確認済み住所の追加は verified_address、address_source を指定。別名は name_variants 配列。過去の市名だけの住所から番地を推測しない。

再照合のみ実行: python hotpepper_refresh.py
テスト: python -m unittest discover -s tests -p 'test_hotpepper*.py'
